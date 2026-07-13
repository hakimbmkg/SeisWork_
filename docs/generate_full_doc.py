#!/usr/bin/env python3
"""
SeisWork - Full Documentation Generator (Markdown)
Author : HakimBMKG

Combines curated docs (install, requirements, debug/errors, module descriptions)
with a FUNCTION REFERENCE auto-extracted from the `seiswork/` source via AST.
Re-runnable: run again whenever code changes to keep the reference in sync.

Output:
    docs/SeisWork_Dokumentasi_Lengkap.md

Run:
    PYTHONNOUSERSITE=1 /opt/miniconda3/envs/seiswork/bin/python docs/generate_full_doc.py
"""

import ast
import base64
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DOCS = BASE / "docs"
sys.path.insert(0, str(DOCS))
PKG = BASE / "seiswork"

_MONTHS = ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]


def _first_commit_date() -> str:
    """Repo's first commit date, so the doc's "Created" date tracks the
    project's actual origin, not today (this generator reruns constantly).
    Formatted by hand (not strftime's locale-dependent %B) to stay English
    regardless of the machine's locale."""
    try:
        out = subprocess.run(
            ["git", "-C", str(BASE), "log", "--reverse", "--format=%ad", "--date=format:%Y-%m-%d"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()
        d = datetime.strptime(out[0], "%Y-%m-%d")
        return f"{d.day:02d} {_MONTHS[d.month - 1]} {d.year}"
    except Exception:
        return datetime.now().strftime("%d %B %Y")


CREATED_DATE = _first_commit_date()

try:
    from seiswork import __version__ as SW_VERSION
except Exception:
    SW_VERSION = "?"

# ── CSS for PDF export (weasyprint) ────────────────────────────────────────
PDF_CSS = """
@page {
  size: A4; margin: 1.8cm 1.9cm 2.0cm 1.9cm;
  @bottom-center { content: "SeisWork — Dokumentasi Lengkap · HakimBMKG · " counter(page) "/" counter(pages);
                   font-size: 8pt; color: #888; }
  @top-right { content: "BMKG — Seismological Processing Framework"; font-size: 7.5pt; color: #aaa; }
}
* { box-sizing: border-box; }
body { font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif; font-size: 9.6pt;
       line-height: 1.55; color: #232323; }
h1 { font-size: 21pt; color: #0a2342; border-bottom: 3px solid #2563a8; padding-bottom: 6px;
     margin: 0 0 4px; }
h2 { font-size: 14.5pt; color: #0a2342; border-bottom: 1.5px solid #cdd9e8; padding-bottom: 3px;
     margin: 20px 0 8px; page-break-after: avoid; }
h3 { font-size: 11.5pt; color: #1a4a7a; margin: 13px 0 5px; page-break-after: avoid; }
p, li { margin: 3px 0; }
code { font-family: 'JetBrains Mono', 'Consolas', monospace; font-size: 8.4pt;
       background: #eef2f7; color: #b8002f; padding: 1px 4px; border-radius: 3px; }
pre { background: #0f1b2d; color: #e6edf3; padding: 9px 12px; border-radius: 6px;
      font-family: 'JetBrains Mono', 'Consolas', monospace; font-size: 8pt; line-height: 1.45;
      page-break-inside: avoid; max-width: 100%;
      /* long source lines must WRAP, not overflow — weasyprint (PDF) has no
         horizontal scroll, so overflow-x:auto used to run code off the page edge */
      white-space: pre-wrap; word-break: break-all; overflow-wrap: anywhere; }
pre code { background: none; color: inherit; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 8.4pt;
        page-break-inside: avoid; table-layout: fixed; }
td, th { word-break: break-word; overflow-wrap: anywhere; }
th { background: #1a4a7a; color: #fff; text-align: left; padding: 4px 7px; }
td { border: 1px solid #d4dcea; padding: 3px 7px; vertical-align: top; }
tr:nth-child(even) td { background: #f5f8fc; }
blockquote { border-left: 3px solid #2563a8; background: #f0f5fb; margin: 7px 0; padding: 5px 12px;
             color: #33506e; font-size: 9pt; }
a { color: #2563a8; text-decoration: none; }
hr { border: none; border-top: 1px solid #d4dcea; margin: 14px 0; }
details.fn-src { margin: 3px 0; border: 1px solid #e1e7f0; border-radius: 5px; }
details.fn-src summary { cursor: pointer; padding: 4px 8px; font-size: 8.6pt;
  background: #f5f8fc; border-radius: 5px; list-style: revert; }
details.fn-src summary code { background: none; color: #1a4a7a; padding: 0; font-weight: 600; }
details.fn-src summary .fn-desc { color: #55637a; font-weight: 400; margin-left: 4px; }
details.fn-src pre { margin: 0; border-radius: 0 0 5px 5px; font-size: 7.6pt; }
/* weasyprint has no interactivity — PDF always renders <details> content open */
.doc-author-block, .doc-author-block * { font-weight: 700; }
.doc-author-block { margin: 10px 0; }
.doc-author-block ol { margin: 2px 0 2px 20px; }
.doc-author-block a { color: #2563a8; }
.doc-version-tag { margin: -2px 0 0; font-size: 9pt; font-weight: 600;
  color: #6b7f9e; letter-spacing: .3px; }
"""

# ── Loading overlay (HTML view only). This file embeds 800+ function source
#   blocks (a few MB of markup), so parsing/layout can take a visible moment
#   on a slow machine. Shown first in <body>, hidden once window 'load' fires.
#   No real progress is knowable for a static-file GET, so the bar is an
#   honest indeterminate sweep, not a fake percentage. ───────────────────────
LOADING_CSS = """
:root {
  --doc-load-bg: #0a2342; --doc-load-title: #fff;
  --doc-load-track: rgba(255,255,255,.15); --doc-load-msg: #b7c6de;
}
@media (prefers-color-scheme: light) {
  :root {
    --doc-load-bg: #f4f7fb; --doc-load-title: #0a2342;
    --doc-load-track: rgba(10,35,66,.12); --doc-load-msg: #55637a;
  }
}
@media print { .doc-loading-ovl { display: none !important; } }
.doc-loading-ovl {
  position: fixed; inset: 0; z-index: 999999;
  background: var(--doc-load-bg); display: flex; align-items: center; justify-content: center;
  transition: opacity .35s ease, background .2s ease;
}
.doc-loading-ovl.doc-loading-hide { opacity: 0; pointer-events: none; }
.doc-loading-title { font-size: 1.5rem; font-weight: 700; color: var(--doc-load-title);
  text-align: center; margin-bottom: 16px; letter-spacing: .4px; }
.doc-loading-track { width: 260px; height: 6px; margin: 0 auto;
  background: var(--doc-load-track); border-radius: 4px; overflow: hidden; }
.doc-loading-bar { height: 100%; width: 40%; border-radius: 4px;
  background: linear-gradient(90deg,#2563a8,#60a5fa);
  animation: doc-loading-sweep 1.1s ease-in-out infinite; }
@keyframes doc-loading-sweep { 0% { margin-left: -40%; } 100% { margin-left: 100%; } }
.doc-loading-msg { margin-top: 10px; font-size: .78rem; color: var(--doc-load-msg); text-align: center; }
"""

def _loading_html(lang: str) -> str:
    msg = "Loading documentation…" if lang == "en" else "Memuat dokumentasi…"
    return (
        '<div class="doc-loading-ovl" id="doc-loading-ovl">'
        f'<div><div class="doc-loading-title">SeisWork</div>'
        '<div class="doc-loading-track"><div class="doc-loading-bar"></div></div>'
        f'<div class="doc-loading-msg" id="doc-loading-msg">{msg}</div>'
        '</div></div>'
        '<script>window.addEventListener("load", function () {'
        'var o = document.getElementById("doc-loading-ovl");'
        'if (o) { o.classList.add("doc-loading-hide"); setTimeout(function () { o.remove(); }, 400); }'
        '});</script>'
    )


# ── Search bar (HTML view only; weasyprint PDFs have no JS and no sticky
#   input). Find-in-page: every match gets a yellow <mark>, ▲/▼ buttons
#   (and Enter/Shift+Enter) jump between them, auto-opening collapsed
#   <details>. Function-reference entries also get filtered so non-matching
#   modules collapse away. ?q=... in the URL runs a search on load. ─────────
SEARCH_CSS = """
@media print { .doc-search-bar { display: none !important; } }
.doc-search-bar {
  position: sticky; top: 0; z-index: 100;
  display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
  background: #0a2342; padding: 10px 18px; margin: 0 0 14px 0; border-radius: 0 0 8px 8px;
}
.doc-search-bar input {
  flex: 1 1 240px; max-width: 420px; font-size: 9.5pt; padding: 6px 10px;
  border: none; border-radius: 5px; outline: none; box-sizing: border-box;
}
.doc-search-bar #doc-search-count {
  font-size: 8pt; color: #b7c6de; font-family: 'JetBrains Mono', 'Consolas', monospace;
  white-space: nowrap;
}
.doc-search-bar .doc-search-nav {
  display: inline-flex; align-items: center; justify-content: center;
  width: 26px; height: 26px; font-size: 10pt; line-height: 1;
  color: #cfe0ff; background: #12305c; border: 1px solid #2d4a75;
  border-radius: 5px; cursor: pointer; user-select: none;
}
.doc-search-bar .doc-search-nav:hover { background: #1b3f74; }
.doc-search-bar .doc-lang-toggle {
  margin-left: auto; font-size: 8.4pt; color: #cfe0ff; white-space: nowrap;
  padding: 4px 8px; border: 1px solid #2d4a75; border-radius: 5px;
}
.doc-search-bar .doc-lang-toggle:hover { background: #12305c; }
mark.doc-hit     { background: #ffe066; color: inherit; padding: 0 1px; border-radius: 2px; }
mark.doc-hit-cur { background: #ff9632; outline: 2px solid #e8531a; }
"""

def _search_bar_html(lang: str) -> str:
    """Sticky bar: find-in-page search (yellow highlights + prev/next) + EN/ID
    language toggle (mirrors the web GUI's own EN/ID switch, see
    seiswork/web/static/js/modules/i18n.js). The toggle is a plain link
    (?lang=...) rather than a JS overlay, since this doc's prose is
    paragraph-scale; two fully authored language variants are more reliable
    than live DOM rewriting."""
    other = "id" if lang == "en" else "en"
    label = "Bahasa Indonesia" if lang == "en" else "English"
    placeholder = ("Search the document… (function, module, error text)" if lang == "en"
                   else "Cari di dokumen… (fungsi, modul, teks error)")
    t_prev = "Previous match (Shift+Enter)" if lang == "en" else "Hasil sebelumnya (Shift+Enter)"
    t_next = "Next match (Enter)" if lang == "en" else "Hasil berikutnya (Enter)"
    return (
        '<div class="doc-search-bar">'
        f'<input type="text" id="doc-search-input" placeholder="{placeholder}" autocomplete="off">'
        '<span id="doc-search-count"></span>'
        f'<span class="doc-search-nav" id="doc-search-prev" title="{t_prev}">▲</span>'
        f'<span class="doc-search-nav" id="doc-search-next" title="{t_next}">▼</span>'
        f'<a class="doc-lang-toggle" href="?lang={other}">🌐 {label}</a>'
        '</div>'
    )

def _search_js(lang: str) -> str:
    no_match = "no matches" if lang == "en" else "tidak ada hasil"
    return f"""
<script>
(function () {{
  var input = document.getElementById('doc-search-input');
  var count = document.getElementById('doc-search-count');
  var btnPrev = document.getElementById('doc-search-prev');
  var btnNext = document.getElementById('doc-search-next');
  var entries = Array.prototype.slice.call(document.querySelectorAll('details.fn-src'));
  var marks = [], cur = -1, timer = null;
  var MAX_MARKS = 2000;   // safety cap — the doc body is ~1.7 MB of text

  // Group each module's non-details "context" (its ### heading, description,
  // docstring, class labels) with the <details> that follow it, up to the
  // next h3 — so non-matching modules collapse out of the way entirely.
  var moduleGroups = [];
  document.querySelectorAll('h3').forEach(function (h3) {{
    var ctx = [], details = [];
    var el = h3.nextElementSibling;
    while (el && el.tagName !== 'H3') {{
      (el.tagName === 'DETAILS' && el.classList.contains('fn-src') ? details : ctx).push(el);
      el = el.nextElementSibling;
    }}
    if (details.length) moduleGroups.push({{ h3: h3, ctx: ctx, details: details }});
  }});

  function clearMarks() {{
    marks.forEach(function (m) {{
      var parent = m.parentNode;
      if (!parent) return;
      parent.replaceChild(document.createTextNode(m.textContent), m);
      parent.normalize();
    }});
    marks = []; cur = -1;
  }}

  function isHidden(el) {{
    for (var p = el.parentElement; p; p = p.parentElement) {{
      if (p.style && p.style.display === 'none') return true;
    }}
    return false;
  }}

  // Wrap every occurrence of q (case-insensitive) in the document body in a
  // yellow <mark class="doc-hit"> — this is the "highlight" the plain
  // entry-filter never provided; without it the bar only reported a count
  // and the page looked untouched.
  function highlight(q) {{
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {{
      acceptNode: function (node) {{
        if (!node.nodeValue || node.nodeValue.toLowerCase().indexOf(q) === -1)
          return NodeFilter.FILTER_REJECT;
        var t = node.parentElement && node.parentElement.tagName;
        if (t === 'SCRIPT' || t === 'STYLE' || t === 'MARK') return NodeFilter.FILTER_REJECT;
        if (node.parentElement.closest('.doc-search-bar')) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }}
    }});
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (var i = 0; i < nodes.length && marks.length < MAX_MARKS; i++) {{
      var node = nodes[i];
      var text = node.nodeValue, lower = text.toLowerCase();
      var frag = document.createDocumentFragment(), pos = 0, idx;
      while ((idx = lower.indexOf(q, pos)) !== -1 && marks.length < MAX_MARKS) {{
        if (idx > pos) frag.appendChild(document.createTextNode(text.slice(pos, idx)));
        var m = document.createElement('mark');
        m.className = 'doc-hit';
        m.textContent = text.slice(idx, idx + q.length);
        frag.appendChild(m);
        marks.push(m);
        pos = idx + q.length;
      }}
      if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
      node.parentNode.replaceChild(frag, node);
    }}
  }}

  function goto(delta) {{
    // Navigate visible marks only (skip those inside modules the filter hid);
    // a mark inside a collapsed <details> is fine — it is opened on arrival.
    var visible = marks.filter(function (m) {{ return !isHidden(m); }});
    if (!visible.length) return;
    var curMark = (cur >= 0 && cur < marks.length) ? marks[cur] : null;
    var vIdx = curMark ? visible.indexOf(curMark) : -1;
    vIdx = (vIdx + delta + visible.length) % visible.length;
    if (curMark) curMark.classList.remove('doc-hit-cur');
    var m = visible[vIdx];
    cur = marks.indexOf(m);
    m.classList.add('doc-hit-cur');
    for (var d = m.closest('details'); d; d = d.parentElement && d.parentElement.closest('details'))
      d.open = true;
    m.scrollIntoView({{ block: 'center' }});
    count.textContent = (vIdx + 1) + ' / ' + visible.length;
  }}

  function apply() {{
    var q = input.value.trim().toLowerCase();
    clearMarks();
    // Filter the function-reference entries (as before) …
    entries.forEach(function (d) {{
      d.style.display = (!q || d.textContent.toLowerCase().indexOf(q) !== -1) ? '' : 'none';
    }});
    moduleGroups.forEach(function (g) {{
      var anyVisible = !q || g.details.some(function (d) {{ return d.style.display !== 'none'; }});
      g.h3.style.display = anyVisible ? '' : 'none';
      g.ctx.forEach(function (el) {{ el.style.display = anyVisible ? '' : 'none'; }});
    }});
    // … then highlight every match in the whole document.
    if (q.length >= 2) {{
      highlight(q);
      var visible = marks.filter(function (m) {{ return !isHidden(m); }});
      count.textContent = visible.length
        ? '0 / ' + visible.length + (marks.length >= MAX_MARKS ? '+' : '')
        : '{no_match}';
      if (visible.length) goto(1);
    }} else {{
      count.textContent = '';
    }}
  }}

  input.addEventListener('input', function () {{
    clearTimeout(timer);
    timer = setTimeout(apply, 300);   // debounce — highlighting walks the whole DOM
  }});
  input.addEventListener('keydown', function (e) {{
    if (e.key === 'Enter') {{ e.preventDefault(); goto(e.shiftKey ? -1 : 1); }}
  }});
  btnNext.addEventListener('click', function () {{ goto(1); }});
  btnPrev.addEventListener('click', function () {{ goto(-1); }});
  document.addEventListener('keydown', function (e) {{
    if (e.key === 'Escape' && document.activeElement === input) {{ input.value = ''; apply(); input.blur(); }}
  }});

  // Deep-linkable search: /help?q=phasenet runs the search on load.
  var qp = new URLSearchParams(location.search).get('q');
  if (qp) {{ input.value = qp; apply(); }}
}})();
</script>
"""

# ── Short module descriptions (curated), English (default) + Indonesian ───
MODULE_DESC_EN = {
    "seiswork/__init__.py": "Package metadata (version).",
    "seiswork/cli.py": "CLI entry point — defines every subcommand (info, setup, "
        "pick, associate, velocity, locate, magnitude, relocate, detect, plot, gui, "
        "remote, full) and the `main()` function. Automatically prepends `core/bin/` to PATH.",
    "seiswork/client.py": "Federation client — `SeisWorkClient` talks to another SeisWork "
        "server over the REST API (bearer-token), used by the `remote` command.",
    "seiswork/parallel.py": "Chunked parallel pipeline orchestrator (`run_parallel`): pick "
        "once (GPU, memory-bounded) → split picks by time chunk → associate+locate+magnitude "
        "in parallel per chunk (ProcessPool spawn) → merge → relocate once. Fixes OOM on long runs.",
    "seiswork/modules/picker/phasenet.py": "PhaseNet picker (seisbench/PyTorch GPU). Serial "
        "I/O→GPU→extract batching, memory-bounded; reads SDS directly; measures P/S amplitude; "
        "optional multi-process reader (`io_processes`).",
    "seiswork/modules/picker/eqt.py": "EQTransformer picker — thin subclass of PhaseNetPicker "
        "(model=EQTransformer). Inherits the whole pipeline (GPU, amplitude, SDS).",
    "seiswork/modules/picker/phasenet_native.py": "Original PhaseNet picker (TensorFlow predict.py) "
        "via Docker. GPU-bound; globs fname.csv from SDS.",
    "seiswork/modules/picker/stalta.py": "Classic STA/LTA picker (no deep learning). "
        "Reads SDS/file, detects triggers, measures peak-to-peak/2 amplitude.",
    "seiswork/modules/associator/gamma.py": "GaMMA phase association (BGMM + DBSCAN pre-clustering). "
        "Picks → events. Converts counts→m/s amplitude via inventory sensitivity.",
    "seiswork/modules/associator/real.py": "REAL phase association (grid search). Picks → events "
        "via the compiled REAL binary.",
    "seiswork/modules/locator/nlloc.py": "NonLinLoc probabilistic location (grid travel-time). "
        "Dual global/jailolo profile.",
    "seiswork/modules/locator/locsat.py": "LocSAT location (SeisComP scolv/scautoloc style).",
    "seiswork/modules/locator/hypoinverse.py": "HYPOINVERSE location (hyp1.40).",
    "seiswork/modules/magnitude/ml.py": "Local ML magnitude (Wood-Anderson, Hutton & Boore "
        "1987). ObsPy WA simulation; internal multi-process Pool.",
    "seiswork/modules/velocity/velest.py": "1D VELEST velocity-model inversion (SimulFlow). "
        "Mode 0=full inversion, 1=location only.",
    "seiswork/modules/relocation/hypodd.py": "HypoDD relative relocation (ph2dt + hypoDD). "
        "Differential-time catalog & cross-correlation.",
    "seiswork/modules/relocation/growclust.py": "GrowClust relative relocation (hierarchical "
        "clustering + relocation). Optional FDTCC dt.cc.",
    "seiswork/modules/relocation/fdtcc.py": "FDTCC — computes differential time via cross-"
        "correlation (dt.cc) for HypoDD/GrowClust.",
    "seiswork/modules/detection/matchlocate.py": "Match&Locate template matching "
        "(MatchLocate2/SelectFinal) — detects new events from a template.",
    "seiswork/modules/preparation/catalog.py": "Catalog preparation (build/merge catalogs).",
    "seiswork/modules/preparation/download.py": "Download waveform/metadata (FDSN) & tools.",
    "seiswork/utils/catalog_filter.py": "Catalog filter (filter_pha-style criteria: mag/RMS/"
        "gap/min-phase/region/time) + ghost-event QC via inter-station cross-correlation "
        "(`min_cc`): `compute_event_cc`, `station_cc_matrix`, `station_stack` — shared by the "
        "Filter Catalog modal and the CC & Stack panels (Result Viewer + live event modal).",
    "seiswork/utils/converter.py": "Format conversion: catalog→phaseSA, station df loader, "
        "catalog→hypodd phase, etc.",
    "seiswork/utils/plotter.py": "Map & figure plotting (cartopy): seismicity map, cross-section.",
    "seiswork/utils/sds_lite.py": "SDSLiteReader — lightweight BotListener-style MSEED SDS "
        "reader (`sds_lite` option, for small windows/repicking).",
    "seiswork/utils/mseedlite.py": "Pure-Python MSEED header parser (GFZ mseedlite clone, GPLv3).",
    "seiswork/web/app.py": "Flask Web GUI server (port 5000) — every REST API route, "
        "picking/pipeline job management, live-reload/hot-reload, federation, sys monitor.",
    "seiswork/web/_pick_runner.py": "Picking subprocess runner (`__main__` guard, spawn-safe).",
    "seiswork/web/_pipeline_runner.py": "Pipeline subprocess runner (assoc/locate/magnitude/"
        "velocity/relocation/detect).",
    "seiswork/web/_full_pipeline.py": "Full end-to-end pipeline runner from the GUI.",
    "seiswork/web/_benchmark.py": "Method-comparison benchmark (for the GUI benchmark modal).",
    "seiswork/web/_residual.py": "HypoDD residual report (compute_residual_report): reads "
        "hypoDD.res/.log/.inp → P/S statistics, histogram, diagnosis.",
}

MODULE_DESC_ID = {
    "seiswork/__init__.py": "Metadata paket (versi).",
    "seiswork/cli.py": "Entry point CLI — mendefinisikan semua subcommand (info, setup, "
        "pick, associate, velocity, locate, magnitude, relocate, detect, plot, gui, "
        "remote, full) dan fungsi `main()`. Otomatis menambahkan `core/bin/` ke PATH.",
    "seiswork/client.py": "Klien federasi — `SeisWorkClient` untuk berkomunikasi dengan "
        "server SeisWork lain via REST API (bearer-token), dipakai command `remote`.",
    "seiswork/parallel.py": "Orkestrator pipeline paralel chunked (`run_parallel`): pick "
        "1x (GPU, memory-bounded) → split picks per-chunk waktu → associate+locate+magnitude "
        "paralel per-chunk (ProcessPool spawn) → merge → relocate 1x. Fix OOM run panjang.",
    "seiswork/modules/picker/phasenet.py": "Picker PhaseNet (seisbench/PyTorch GPU). Batch "
        "serial I/O→GPU→extract, memory-bounded; baca SDS langsung; ukur amplitudo P/S; "
        "opsi multi-process reader (`io_processes`).",
    "seiswork/modules/picker/eqt.py": "Picker EQTransformer — subclass tipis PhaseNetPicker "
        "(model=EQTransformer). Mewarisi seluruh pipeline (GPU, amplitudo, SDS).",
    "seiswork/modules/picker/phasenet_native.py": "Picker PhaseNet asli (TensorFlow predict.py) "
        "via Docker. GPU-bound; glob fname.csv dari SDS.",
    "seiswork/modules/picker/stalta.py": "Picker klasik STA/LTA (tanpa deep learning). "
        "Baca SDS/file, deteksi trigger, ukur amplitudo peak-to-peak/2.",
    "seiswork/modules/associator/gamma.py": "Asosiasi fase GaMMA (BGMM + pra-klaster DBSCAN). "
        "Picks → event. Konversi amplitudo counts→m/s via sensitivitas inventory.",
    "seiswork/modules/associator/real.py": "Asosiasi fase REAL (grid-search). Picks → event "
        "via binary REAL terkompilasi.",
    "seiswork/modules/locator/nlloc.py": "Lokasi probabilistik NonLinLoc (grid travel-time). "
        "Dual profile global/jailolo.",
    "seiswork/modules/locator/locsat.py": "Lokasi LocSAT (SeisComP scolv/scautoloc style).",
    "seiswork/modules/locator/hypoinverse.py": "Lokasi HYPOINVERSE (hyp1.40).",
    "seiswork/modules/magnitude/ml.py": "Magnitudo lokal ML (Wood-Anderson, Hutton & Boore "
        "1987). ObsPy simulasi WA; Pool internal multi-proses.",
    "seiswork/modules/velocity/velest.py": "Inversi model kecepatan 1D VELEST (SimulFlow). "
        "Mode 0=full inversion, 1=lokasi saja.",
    "seiswork/modules/relocation/hypodd.py": "Relokasi relatif HypoDD (ph2dt + hypoDD). "
        "Differential-time katalog & cross-correlation.",
    "seiswork/modules/relocation/growclust.py": "Relokasi relatif GrowClust (clustering "
        "hierarkis + relokasi). Opsi FDTCC dt.cc.",
    "seiswork/modules/relocation/fdtcc.py": "FDTCC — hitung differential time via cross-"
        "correlation (dt.cc) untuk HypoDD/GrowClust.",
    "seiswork/modules/detection/matchlocate.py": "Template matching Match&Locate "
        "(MatchLocate2/SelectFinal) — deteksi event baru dari template.",
    "seiswork/modules/preparation/catalog.py": "Persiapan katalog (build/merge katalog).",
    "seiswork/modules/preparation/download.py": "Download waveform/metadata (FDSN) & tool.",
    "seiswork/utils/catalog_filter.py": "Filter katalog (kriteria ala filter_pha: mag/RMS/"
        "gap/min-phase/region/waktu) + QC ghost-event via cross-correlation antar-stasiun "
        "(`min_cc`): `compute_event_cc`, `station_cc_matrix`, `station_stack` — dipakai bersama "
        "modal Filter Catalog dan panel CC & Stack (Result Viewer + modal event live).",
    "seiswork/utils/converter.py": "Konversi format: catalog→phaseSA, station df loader, "
        "catalog→hypodd phase, dll.",
    "seiswork/utils/plotter.py": "Plot peta & figur (cartopy): seismicity map, cross-section.",
    "seiswork/utils/sds_lite.py": "SDSLiteReader — baca MSEED SDS ringan ala BotListener "
        "(opsi `sds_lite`, untuk jendela kecil/repick).",
    "seiswork/utils/mseedlite.py": "Parser header MSEED pure-Python (klon GFZ mseedlite, GPLv3).",
    "seiswork/web/app.py": "Server Flask Web GUI (port 5000) — semua route REST API, "
        "manajemen job picking/pipeline, live-reload/hot-reload, federasi, sys monitor.",
    "seiswork/web/_pick_runner.py": "Runner subprocess picking (guard __main__, spawn-safe).",
    "seiswork/web/_pipeline_runner.py": "Runner subprocess pipeline (assoc/locate/magnitude/"
        "velocity/relocation/detect).",
    "seiswork/web/_full_pipeline.py": "Runner pipeline penuh end-to-end dari GUI.",
    "seiswork/web/_benchmark.py": "Benchmark perbandingan metode (untuk modal benchmark GUI).",
    "seiswork/web/_residual.py": "Laporan residual HypoDD (compute_residual_report): baca "
        "hypoDD.res/.log/.inp → statistik P/S, histogram, diagnosis.",
}

# ── Curated sections (markdown) ────────────────────────────────────────────

OVERVIEW_EN = """\
## 1. Overview & Architecture

<!-- INJECT_DIAGRAMS -->


**SeisWork** is a unified Python framework for automated seismological data
processing — from phase picking through relocation and magnitude — that
integrates many tools (PhaseNet/EQTransformer, GaMMA, REAL, NonLinLoc,
LocSAT, HYPOINVERSE, VELEST, HypoDD, GrowClust, FDTCC, Match&Locate) under
one conda env, one installer, one test suite, plus a Web GUI.

### Pipeline

```
Waveform (SDS / FDSN)
   └── Pick      : PhaseNet / EQTransformer / STA-LTA   → picks.csv
         └── Associate : GaMMA (BGMM+DBSCAN) / REAL      → catalog_*.csv
               └── Velocity : VELEST (1D model)
               └── Locate   : NonLinLoc / LocSAT / HYPOINVERSE
                     └── Magnitude : ML Wood-Anderson (Hutton & Boore 1987)
                           └── Relocate : HypoDD / GrowClust (+FDTCC dt.cc)
                           └── Detect   : Match&Locate (template matching)
                                 └── Plot : maps, cross-sections, statistics
```

### Directory structure

```
seiswork/                 ← project root (BASE_DIR)
├── pyproject.toml        ← pip package definition
├── install.sh            ← Linux installer (7 steps)
├── install_mac.sh        ← macOS installer
├── environment.yml       ← conda dependencies
├── requirements.txt      ← pip dependencies (subset)
├── test_seiswork.py      ← test suite
├── config/config.yaml    ← runtime configuration
├── core/
│   ├── src/              ← tool source (REAL, HypoDD, NonLinLoc, GrowClust)
│   └── bin/              ← compiled ELF binaries  (~/bin/ = symlink here)
├── seiswork/            ← Python package
│   ├── cli.py           ← every command + main()
│   ├── client.py        ← federation client (remote)
│   ├── parallel.py      ← chunked parallel orchestrator
│   ├── modules/{picker,associator,velocity,locator,magnitude,relocation,detection,preparation}/
│   ├── utils/{converter,plotter,sds_lite,mseedlite}.py
│   └── web/             ← Flask Web GUI (app.py + runner + helpers)
├── docker/              ← native-PhaseNet GPU image (TF)
├── notebooks/           ← scenario/runner notebooks
└── work/               ← pipeline output
```
"""

OVERVIEW_ID = """\
## 1. Ikhtisar & Arsitektur

<!-- INJECT_DIAGRAMS -->


**SeisWork** adalah framework Python terpadu untuk pemrosesan data seismologi
otomatis — dari *phase picking* hingga relokasi dan magnitudo — yang
mengintegrasikan banyak tool (PhaseNet/EQTransformer, GaMMA, REAL, NonLinLoc,
LocSAT, HYPOINVERSE, VELEST, HypoDD, GrowClust, FDTCC, Match&Locate) di bawah
satu *conda env*, satu installer, satu test suite, plus Web GUI.

### Pipeline

```
Waveform (SDS / FDSN)
   └── Pick      : PhaseNet / EQTransformer / STA-LTA   → picks.csv
         └── Associate : GaMMA (BGMM+DBSCAN) / REAL      → catalog_*.csv
               └── Velocity : VELEST (model 1D)
               └── Locate   : NonLinLoc / LocSAT / HYPOINVERSE
                     └── Magnitude : ML Wood-Anderson (Hutton & Boore 1987)
                           └── Relocate : HypoDD / GrowClust (+FDTCC dt.cc)
                           └── Detect   : Match&Locate (template matching)
                                 └── Plot : peta, penampang, statistik
```

### Struktur direktori

```
seiswork/                 ← project root (BASE_DIR)
├── pyproject.toml        ← definisi paket pip
├── install.sh            ← installer Linux (7 step)
├── install_mac.sh        ← installer macOS
├── environment.yml       ← dependensi conda
├── requirements.txt      ← dependensi pip (subset)
├── test_seiswork.py      ← test suite
├── config/config.yaml    ← konfigurasi runtime
├── core/
│   ├── src/              ← source binari (REAL, HypoDD, NonLinLoc, GrowClust)
│   └── bin/              ← binari ELF terkompilasi  (~/bin/ = symlink ke sini)
├── seiswork/            ← paket Python
│   ├── cli.py           ← semua command + main()
│   ├── client.py        ← klien federasi (remote)
│   ├── parallel.py      ← orkestrator paralel chunked
│   ├── modules/{picker,associator,velocity,locator,magnitude,relocation,detection,preparation}/
│   ├── utils/{converter,plotter,sds_lite,mseedlite}.py
│   └── web/             ← Flask Web GUI (app.py + runner + helper)
├── docker/              ← image PhaseNet-native GPU (TF)
├── notebooks/           ← notebook skenario & runner
└── work/               ← output pipeline
```
"""

REQUIREMENTS_EN = """\
## 2. System Requirements

### 2.1 Operating system & build toolchain
Needed to compile the seismological binaries (`install.sh` Steps 1 & 3):

| Tool | Purpose |
|------|----------|
| `gcc` | compiles REAL (C) |
| `gfortran` | compiles HypoDD, ph2dt, GrowClust, VELEST (Fortran) |
| `cmake` + `make` | compiles NonLinLoc |
| `git`, `wget` | download tool source |

- **Linux** (primary, tested on Ubuntu) → `install.sh`
- **macOS** → `install_mac.sh` (OS-aware)

### 2.2 Conda environment `seiswork`
All C-extension packages go through **conda-forge** (`environment.yml`):

- Scientific core: `python=3.10`, `numpy`, `pandas`, `scipy`, `matplotlib`, `tqdm`, `pyyaml`
- Seismology: `obspy`, `pyproj`, `cartopy`, `shapely`, `geopandas`
- Association: `scikit-learn` (GaMMA backend), `numba`, `llvmlite`
- System: `psutil` (sys-monitor GUI)
- Pip: `seisbench`, `flask`, `requests`

> **IMPORTANT — the env lives at `/opt/miniconda3/envs/seiswork`** (NOT `~/miniconda3`).
> Always set `PYTHONNOUSERSITE=1` and (for MKL modules) `MKL_THREADING_LAYER=GNU`.

### 2.3 PyTorch + CUDA (deep-learning pickers)
Installed via pip in Step 5 (CUDA detected at runtime):

- **PyTorch cu121** (`--index-url .../whl/cu121`) + `PYTHONNOUSERSITE=1`
  (NOT conda pytorch — MKL/iJIT conflict; NOT cu130 — CUDA 12.2 driver unsupported)
- **seisbench** replaces TF PhaseNet (PyTorch, avoids TF conflicts)
- **GaMMA from GitHub**: `pip install git+https://github.com/AI4EPS/GaMMA.git`
  (NOT `pip install gamma` — that's an unrelated crystallography package!)
- cuDNN must be **cu12 9.1.0** (see Section 7 — cuDNN cu13 bug)

### 2.4 Compiled binaries (`core/bin/`)

| Binary | Source | Toolchain |
|--------|--------|-----------|
| REAL | core/src/REAL/.../REAL.c | gcc -O2 |
| hypoDD, ph2dt | core/src/HYPODD/ | gfortran (g77→gfortran patch) |
| NLLoc, Grid2Time, Vel2Grid, Time2EQ, PhsAssoc | core/src/NonLinLoc/ | cmake + make |
| growclust | core/src/GrowClust/ | gfortran |
| velest | license-restricted | symlink to SimulFlow |
| hypoinverse | license-restricted | symlink to `~/bin/hyp1.40` |

`~/bin/` holds a symlink to `core/bin/`; `cli.py` automatically prepends `core/bin/` to PATH.

### 2.5 Optional
- **Docker + NVIDIA runtime** — native-PhaseNet GPU image (TF2.12, Hopper cuDNN fix)
- **SeisComP** — for LocSAT & some SCML I/O
- **NVIDIA GPU** (H100/Hopper tested) — deep-learning pickers; CPU still works but slow
"""

REQUIREMENTS_ID = """\
## 2. Kebutuhan Sistem (Requirements)

### 2.1 Sistem Operasi & toolchain build
Diperlukan untuk mengompilasi binari seismologi (Step 1 & 3 `install.sh`):

| Tool | Kegunaan |
|------|----------|
| `gcc` | kompilasi REAL (C) |
| `gfortran` | kompilasi HypoDD, ph2dt, GrowClust, VELEST (Fortran) |
| `cmake` + `make` | kompilasi NonLinLoc |
| `git`, `wget` | unduh source tool |

- **Linux** (utama, diuji Ubuntu) → `install.sh`
- **macOS** → `install_mac.sh` (OS-aware)

### 2.2 Conda environment `seiswork`
Semua paket ber-ekstensi C lewat **conda-forge** (`environment.yml`):

- Inti saintifik: `python=3.10`, `numpy`, `pandas`, `scipy`, `matplotlib`, `tqdm`, `pyyaml`
- Seismologi: `obspy`, `pyproj`, `cartopy`, `shapely`, `geopandas`
- Asosiasi: `scikit-learn` (backend GaMMA), `numba`, `llvmlite`
- Sistem: `psutil` (sys-monitor GUI)
- Pip: `seisbench`, `flask`, `requests`

> **PENTING — env ada di `/opt/miniconda3/envs/seiswork`** (BUKAN `~/miniconda3`).
> Selalu set `PYTHONNOUSERSITE=1` dan (untuk modul MKL) `MKL_THREADING_LAYER=GNU`.

### 2.3 PyTorch + CUDA (picker deep-learning)
Diinstal via pip pada Step 5 (CUDA dideteksi runtime):

- **PyTorch cu121** (`--index-url .../whl/cu121`) + `PYTHONNOUSERSITE=1`
  (BUKAN conda pytorch — konflik MKL/iJIT; BUKAN cu130 — driver CUDA 12.2 tak support)
- **seisbench** menggantikan TF PhaseNet (PyTorch, hindari konflik TF)
- **GaMMA dari GitHub**: `pip install git+https://github.com/AI4EPS/GaMMA.git`
  (BUKAN `pip install gamma` — itu paket kristalografi!)
- cuDNN harus **cu12 9.1.0** (lihat Bagian 7 — bug cuDNN cu13)

### 2.4 Binari terkompilasi (`core/bin/`)

| Binary | Source | Toolchain |
|--------|--------|-----------|
| REAL | core/src/REAL/.../REAL.c | gcc -O2 |
| hypoDD, ph2dt | core/src/HYPODD/ | gfortran (patch g77→gfortran) |
| NLLoc, Grid2Time, Vel2Grid, Time2EQ, PhsAssoc | core/src/NonLinLoc/ | cmake + make |
| growclust | core/src/GrowClust/ | gfortran |
| velest | license-restricted | symlink ke SimulFlow |
| hypoinverse | license-restricted | symlink ke `~/bin/hyp1.40` |

`~/bin/` berisi symlink ke `core/bin/`; `cli.py` otomatis prepend `core/bin/` ke PATH.

### 2.5 Opsional
- **Docker + NVIDIA runtime** — image PhaseNet-native GPU (TF2.12, fix cuDNN Hopper)
- **SeisComP** — untuk LocSAT & beberapa I/O SCML
- **GPU NVIDIA** (H100/Hopper diuji) — picker deep-learning; CPU tetap jalan tapi lambat
"""

INSTALL_EN = """\
## 3. Installation

### 3.1 Automatic (recommended)

```bash
git clone https://github.com/hakimbmkg/seiswork.git
cd seiswork
bash install.sh            # full install: OS check → download → compile → env → pip → verify → docker
conda activate seiswork
```

`install.sh` runs **7 steps**:

| Step | Action |
|------|------|
| 1 | Check OS dependencies (gcc, gfortran, cmake, git, wget) |
| 2 | Download source (REAL, HypoDD, NonLinLoc, GrowClust) |
| 3 | Compile binaries → `core/bin/` + symlink `~/bin/` |
| 4 | Create the `seiswork` conda env from `environment.yml` |
| 5 | Install Python packages (auto-CUDA PyTorch, seisbench, GaMMA) |
| 6 | Verify the installation |
| 7 | Docker + native-PhaseNet GPU image (optional, OS-aware) |

Options:
```bash
bash install.sh --check        # status summary only
bash install.sh --step 3       # rerun a single step
bash install.sh --step 4,5     # rerun env + Python
```

macOS: `bash install_mac.sh`.

### 3.2 Python package (dev mode)
```bash
conda activate seiswork
pip install -e .               # editable; no reinstall needed when editing code
```
Entry point: `seiswork <command>` (or `python seiswork.py <command>` — compat launcher).

### 3.3 Verification
```bash
seiswork info                  # show tool availability & pipeline overview
python test_seiswork.py        # test suite
```
"""

INSTALL_ID = """\
## 3. Instalasi

### 3.1 Otomatis (disarankan)

```bash
git clone https://github.com/hakimbmkg/seiswork.git
cd seiswork
bash install.sh            # full install: OS check → download → compile → env → pip → verify → docker
conda activate seiswork
```

`install.sh` menjalankan **7 step**:

| Step | Aksi |
|------|------|
| 1 | Cek dependensi OS (gcc, gfortran, cmake, git, wget) |
| 2 | Download source (REAL, HypoDD, NonLinLoc, GrowClust) |
| 3 | Compile binari → `core/bin/` + symlink `~/bin/` |
| 4 | Buat conda env `seiswork` dari `environment.yml` |
| 5 | Install paket Python (PyTorch auto-CUDA, seisbench, GaMMA) |
| 6 | Verifikasi instalasi |
| 7 | Docker + image PhaseNet-native GPU (opsional, OS-aware) |

Opsi:
```bash
bash install.sh --check        # ringkasan status saja
bash install.sh --step 3       # ulang satu step
bash install.sh --step 4,5     # ulang env + Python
```

macOS: `bash install_mac.sh`.

### 3.2 Paket Python (mode dev)
```bash
conda activate seiswork
pip install -e .               # editable; tak perlu reinstall saat edit kode
```
Entry point: `seiswork <command>` (atau `python seiswork.py <command>` — launcher kompat).

### 3.3 Verifikasi
```bash
seiswork info                  # tampilkan ketersediaan tool & ikhtisar pipeline
python test_seiswork.py        # test suite
```
"""

USAGE_EN = """\
## 4. Usage

### 4.1 CLI commands

| Command | Function | Method |
|---------|--------|--------|
| `seiswork info` | Tool availability & overview | — |
| `seiswork setup` | Initialize workspace | — |
| `seiswork pick` | Phase picking | `phasenet`, `eqt`, `stalta` |
| `seiswork associate` | Associate picks→events | `gamma`, `real` |
| `seiswork velocity` | 1D velocity model | `velest` (mode 0/1) |
| `seiswork locate` | Hypocenter location | `nlloc`, `locsat`, `hypoinverse`, `all` |
| `seiswork magnitude` | ML Wood-Anderson | — |
| `seiswork relocate` | Relative relocation | `catalog`(HypoDD), `crosscorr`, `growclust` |
| `seiswork detect` | Template matching | `matchlocate` |
| `seiswork plot` | Maps & figures | `--step all` |
| `seiswork full` | End-to-end pipeline | `--start/--end` |
| `seiswork gui` | Web GUI (port 5000) | — |
| `seiswork remote` | Server-client federation | — |

### 4.2 Web GUI

```bash
PYTHONNOUSERSITE=1 MKL_THREADING_LAYER=GNU \\
  /opt/miniconda3/envs/seiswork/bin/python seiswork.py gui --port 5000
# or
/opt/miniconda3/envs/seiswork/bin/python -m seiswork.web.app --port 5000
```
Access `http://<host>:5000`. Features: station configuration on a Leaflet map,
run each pipeline step + streaming log, waveform viewer (drum/3-comp), Result
Viewer (map + cross-section + 3D + residual statistics), Output file browser,
Sys Monitor, live-reload (code edits show up immediately).

Newer GUI features:

- **Activity Log** (navbar, beside Help) — one place for every success/error from
  the whole app: panel alerts (`showAlert`), API calls (failed always; successful
  POST/PUT/DELETE), uncaught JS errors. Ticker shows the latest entry; click →
  history panel (newest first, source tag, repeated entries collapsed ×N).
  Aborted fetches (poll timeout/superseded) are NOT logged — routine, not errors.
- **CC & Stack** — inter-station cross-correlation + aligned/stacked waveform
  around the P pick, same QC as `notebooks/event_station_stack_xcorr.ipynb`.
  Toggle in the Result Viewer "Waveform & Picks" modal (full CC matrix + stack)
  and in the online live event modal (stack only; mean CC in the info line).
  Reads SDS (not the ring buffer) — a very fresh live event may not be archived
  yet; retry after slarchive writes it. Results are cached server-side.
- **Min CC filter** (Filter Catalog modal, opt-in toggle) — drops "ghost" events
  whose mean inter-station CC falls below the threshold (default 0.15 = outlier
  floor, tuned on the 7G/Jailolo network; re-tune per network). Events without
  enough waveform evidence are kept, not dropped. Passing events get a `qc_cc`
  column in the output catalog.

**Restarting the server (after changing backend `.py` code):**
```bash
kill $(ps aux | grep "seiswork.web.app" | grep -v grep | awk '{print $2}') 2>/dev/null; sleep 1
/opt/miniconda3/envs/seiswork/bin/python -m seiswork.web.app --port 5000 > /tmp/seiswork.log 2>&1 &
disown
```
> **Frontend** changes (`static/js`, `templates`) do NOT need a restart — they refresh
> automatically via cache-busting (`?v=mtime`) + SSE live-reload. Use
> `SEISWORK_NO_AUTORESTART=1` when a long pipeline job must not be interrupted.

### 4.3 Configuration (`config/config.yaml`)

Top-level skeleton (see `seiswork/cli.py:load_config`):

```
region:      name, lat, lon, lat_min/max, lon_min/max, depth_max, starttime, endtime
data:        waveform_dir, sds_format, station_file, inventory
fdsn:        client, user, password, networks, channels
pick:        phasenet{...}, stalta{...}
associate:   gamma{...}, real{...}
velocity:    velest{...}
locate:      hypoinverse{...}, locsat{...}, nlloc{...}
magnitude:   inventory, dist_max_km, wood_anderson{...}, a_coefficient, b_coefficient
relocation:  hypodd{...}, growclust{...}
detection:   matchlocate{...}
plot:        dpi, figsize, depth_colormap, mag_scale, map_extent
federation:  token, server_url, sync_dir, auto_sync
```
> `mechanism`/`imaging` (SKHASH, FocoNet, SIMUL2000) are NOT in the static `config.yaml` —
> the GUI builds them per-run as a temporary YAML (`_build_pipe_cfg` in `seiswork/web/app.py`).

**Required GaMMA keys** (differ from GaMMA's upstream docs) — see Section 7.3:
```python
config = {
    "dims"             : ["x(km)", "y(km)", "z(km)"],
    "x(km)": (-50, 50), "y(km)": (-50, 50), "z(km)": (0, 60),   # tuple (min, max)
    "bfgs_bounds"      : ((x0-1, x1+1), (y0-1, y1+1), (0, z1+1), (None, None)),
    "min_picks_per_eq" : 8,        # NOT min_picks_per_event (old API)
    "covariance_prior" : [5.0],    # MUST be a list, not a scalar float
    "use_amplitude": True, "use_dbscan": False, "oversample_factor": 5,
}
```
"""

USAGE_ID = """\
## 4. Penggunaan

### 4.1 Command CLI

| Command | Fungsi | Metode |
|---------|--------|--------|
| `seiswork info` | Ketersediaan tool & ikhtisar | — |
| `seiswork setup` | Inisialisasi workspace | — |
| `seiswork pick` | Phase picking | `phasenet`, `eqt`, `stalta` |
| `seiswork associate` | Asosiasi picks→event | `gamma`, `real` |
| `seiswork velocity` | Model kecepatan 1D | `velest` (mode 0/1) |
| `seiswork locate` | Lokasi hiposenter | `nlloc`, `locsat`, `hypoinverse`, `all` |
| `seiswork magnitude` | ML Wood-Anderson | — |
| `seiswork relocate` | Relokasi relatif | `catalog`(HypoDD), `crosscorr`, `growclust` |
| `seiswork detect` | Template matching | `matchlocate` |
| `seiswork plot` | Peta & figur | `--step all` |
| `seiswork full` | Pipeline end-to-end | `--start/--end` |
| `seiswork gui` | Web GUI (port 5000) | — |
| `seiswork remote` | Federasi server-client | — |

### 4.2 Web GUI

```bash
PYTHONNOUSERSITE=1 MKL_THREADING_LAYER=GNU \\
  /opt/miniconda3/envs/seiswork/bin/python seiswork.py gui --port 5000
# atau
/opt/miniconda3/envs/seiswork/bin/python -m seiswork.web.app --port 5000
```
Akses `http://<host>:5000`. Fitur: konfigurasi stasiun di peta Leaflet, jalankan
tiap step pipeline + streaming log, waveform viewer (drum/3-comp), Result Viewer
(peta + penampang + 3D + statistik residual), Output file browser, Sys Monitor,
live-reload (edit kode langsung tersaji).

Fitur GUI terbaru:

- **Activity Log** (navbar, samping Help) — satu tempat untuk semua sukses/error
  dari seluruh app: alert panel (`showAlert`), panggilan API (gagal selalu; sukses
  POST/PUT/DELETE), error JS uncaught. Ticker menampilkan entri terakhir; klik →
  panel riwayat (terbaru dulu, tag sumber, entri berulang dikolaps ×N). Fetch yang
  di-abort (poll timeout/superseded) TIDAK dicatat — rutin, bukan error.
- **CC & Stack** — cross-correlation antar-stasiun + waveform selaras/stack di
  sekitar pick P, QC yang sama dengan `notebooks/event_station_stack_xcorr.ipynb`.
  Toggle di modal "Waveform & Picks" Result Viewer (matriks CC penuh + stack)
  dan di modal event live online (stack saja; mean CC di baris info). Membaca
  SDS (bukan ring buffer) — event live yang sangat baru mungkin belum ter-arsip;
  coba lagi setelah slarchive menulis. Hasil di-cache di server.
- **Filter Min CC** (modal Filter Catalog, toggle opt-in) — buang event "ghost"
  yang mean CC antar-stasiunnya di bawah ambang (default 0.15 = outlier floor,
  di-tuning di jaringan 7G/Jailolo; tuning ulang per jaringan). Event tanpa cukup
  bukti waveform TETAP dipertahankan, tidak dibuang. Event yang lolos mendapat
  kolom `qc_cc` di katalog output.

**Restart server (saat mengubah kode backend `.py`):**
```bash
kill $(ps aux | grep "seiswork.web.app" | grep -v grep | awk '{print $2}') 2>/dev/null; sleep 1
/opt/miniconda3/envs/seiswork/bin/python -m seiswork.web.app --port 5000 > /tmp/seiswork.log 2>&1 &
disown
```
> Perubahan **frontend** (`static/js`, `templates`) TIDAK butuh restart — auto
> via cache-bust (`?v=mtime`) + SSE live-reload. Gunakan `SEISWORK_NO_AUTORESTART=1`
> bila ada job pipeline panjang yang tak boleh terganggu.

### 4.3 Konfigurasi (`config/config.yaml`)

Skeleton top-level (lihat `seiswork/cli.py:load_config`):

```
region:      name, lat, lon, lat_min/max, lon_min/max, depth_max, starttime, endtime
data:        waveform_dir, sds_format, station_file, inventory
fdsn:        client, user, password, networks, channels
pick:        phasenet{...}, stalta{...}
associate:   gamma{...}, real{...}
velocity:    velest{...}
locate:      hypoinverse{...}, locsat{...}, nlloc{...}
magnitude:   inventory, dist_max_km, wood_anderson{...}, a_coefficient, b_coefficient
relocation:  hypodd{...}, growclust{...}
detection:   matchlocate{...}
plot:        dpi, figsize, depth_colormap, mag_scale, map_extent
federation:  token, server_url, sync_dir, auto_sync
```
> `mechanism`/`imaging` (SKHASH, FocoNet, SIMUL2000) TIDAK ada di `config.yaml` statis —
> dibangun per-run sebagai YAML sementara oleh GUI (`_build_pipe_cfg` di `seiswork/web/app.py`).

**Key wajib GaMMA** (beda dari dokumentasi upstream GaMMA) — lihat Bagian 7.3:
```python
config = {
    "dims"             : ["x(km)", "y(km)", "z(km)"],
    "x(km)": (-50, 50), "y(km)": (-50, 50), "z(km)": (0, 60),   # tuple (min, max)
    "bfgs_bounds"      : ((x0-1, x1+1), (y0-1, y1+1), (0, z1+1), (None, None)),
    "min_picks_per_eq" : 8,        # BUKAN min_picks_per_event (API lama)
    "covariance_prior" : [5.0],    # WAJIB list, bukan scalar float
    "use_amplitude": True, "use_dbscan": False, "oversample_factor": 5,
}
```
"""

DEBUG_ERRORS_EN = """\
## 7. Debug, Errors & Fixes

Catalog of issues found in the past + root cause + fix (root-cause fix, not a
workaround). Organized by area.

### 7.1 Environment & CUDA

**cuDNN hangs 30+ minutes → `CUDNN_STATUS_NOT_INITIALIZED`.**
`nvidia-cudnn-cu13` (9.20) overwrote `cu12` (9.1.0); it needs CUDA 13 but the driver is only 12.2.
*Fix:*
```bash
pip uninstall nvidia-cudnn-cu13
pip install --force-reinstall nvidia-cudnn-cu12==9.1.0.70 \\
  nvidia-cublas-cu12==12.1.3.1 nvidia-cuda-nvrtc-cu12==12.1.105
```
Verify `torch.backends.cudnn.version()` = 90100.

**Jupyter kernel SIGSEGV during parallel picking.**
`libmseed` isn't thread-safe — many parallel `read()` threads corrupt its global C state.
*Fix:* `_MSEED_LOCK = threading.Lock()` wraps every on-disk MSEED read (FDSN is safe).

**`libmkl symbol error` / iJIT.** *Fix:* `MKL_THREADING_LAYER=GNU` + `PYTHONNOUSERSITE=1`;
PyTorch via pip (not conda).

### 7.2 Picker

- **Last pick in a file goes missing** → flush `if in_peak:` after the loop.
- **`workers`/`batch_size` unused** → now passed through to `annotate()`.
- **Wrong seisbench channel detection** → check `"_P_" in ch` (not `"P" in ch`).
- **`phase_type` vs `phase_hint`** → the whole pipeline reads `phase_hint`.
- **`phase_amp` always NaN (PhaseNet/EQT)** → `_make_pick` had no waveform access
  (only the probability trace). *Fix:* keep `raw_stream` alive until extraction;
  `_measure_amp` measures peak-to-peak/2 on the FILTERED waveform (P: Z, S: N/E).
- **`proc.wait()` hangs in state D** after the picker finishes (MP cleanup). Picks/log are
  already correct; status.json just updates late. Mitigation: timeout + fallback update from picks.csv.
- **`io_processes` (multi-process reader)**: correct, but only ~6% faster —
  the bottleneck is the rotational HDD `/dev/sda` (~44MB/s seek-thrash), not locking. Don't
  expect a big speedup on this kind of disk.
- **`sds_lite` (pure-Python)**: correct (byte-identical) but **4.6–40× slower** than
  `sds` (SDSClient already reads selectively in libmseed/C). NO-GO for bulk use; only for
  small windows/repicking.

### 7.3 Association (GaMMA / REAL)

- **GaMMA superlinearly slow** → config didn't set `use_dbscan`. *Fix:* `use_dbscan=True,
  dbscan_min_samples=3`, leave `dbscan_eps` empty (auto from aperture÷vel_p).
  >15× speedup (A/B verified).
- **`pip install gamma` installs the wrong package** (crystallography) → use
  `pip install git+https://github.com/AI4EPS/GaMMA.git`.
- **Required GaMMA keys**: `min_picks_per_eq` (not `min_picks_per_event`);
  `covariance_prior` must be a **list** (`[5.0]`).
- **BGMM crashes "Input X contains NaN"** → partially NaN amplitudes. *Fix:* drop
  NaN-amplitude picks when `use_amplitude=True` (check `isna().any()`).
- **GaMMA magnitude over-estimate (mag≈8.0)** → `phase_amp` is in counts vs. GaMMA's
  m/s assumption. *Fix:* convert counts→m/s via inventory sensitivity. Authoritative
  magnitude = ML WA.
- **REAL `FileNotFoundError`** → `os.makedirs(..., exist_ok=True)` for the station dir; the
  same variant also showed up when the LOG directory was overridden by
  `_pipeline_runner.py` but never actually created in `_run_one_day()`.
- **REAL `ValueError: too many values to unpack`** → a 4-element tuple unpacked as 2
  (`for date, _ in ...`). *Fix:* `for date, *_ in ...`.
- **BGMM "hangs" for >1 hour** on 7,357 picks wasn't an algorithm problem — a typo'd
  parameter (`use_amp` instead of `use_amplitude`, so `use_dbscan` never activated). Once
  the parameter was correct + `use_dbscan=True`: 1hr+ → **1.3 seconds**.

### 7.4 Location & relocation (HypoDD / ph2dt / VELEST)

- **getarg path truncation** (ph2dt/hypoDD use `character*30/*80`) → long absolute
  paths get truncated. *Fix:* call the binary with `basename(inp)` + `cwd=work_dir`.
- **`_write_hypodd_inp` wrong format** → getinp.f crashes "End of file". *Fix:* rewrote the
  template to exactly match getinp.f's order (9 filenames → IDAT/IPHA/DIST → OBSCC/OBSCT → ... → VEL → CID).
- **SIGFPE div-by-zero in ph2dt.f** (small catalog → npair=0/nev=0). *Fix:* guard every
  division `if(divisor.gt.0)`, recompile. Lesson: legacy Fortran SIGFPEs are often multi-site —
  use `gdb -batch` to find the exact location.
- **"0 relocated events" isn't a crash** (a short window legitimately has none) →
  `_write_fallback_relocated` falls back to the pre-relocation catalog (`<orig>_no_reloc` method).
- **`_load_station_df` breaks on 6-column FDSN** (empty LOC) → lat becomes NaN.
  *Fix:* auto-detect the lat column offset via range validation.
- **A 1-day catalog is TOO SPARSE** for differential-time methods (HypoDD/GrowClust/crosscorr
  see minimal motion) — a data limitation, not a bug. Match&Locate fits best (finding new events).
- **NLLoc "0 events located"** even though `nlloc.log` says "14 events read" → the
  Jupyter kernel was still caching an old `NLLocLocator` module version (`grid_dir`/`model_prefix`
  from `locate_cfg` ignored). *Fix:* **restart the kernel** after editing a SeisWork module —
  editing a `.py` file doesn't auto-reload an already-imported module.
- **`catalog_nlloc.csv`/`catalog_ml.csv` output hardcoded** to `BASE/work/catalog/...`
  even when the notebook/job has its own working directory → risk of `FileNotFoundError` or
  reading a stale result from a previous run. *Fix:* `catalog_dir` property (alias `cat_dir`) on
  `NLLocLocator` & `MLMagnitude`, mirroring the convention already used in `GammaAssociator`.
- **HypoDD given a `.pha` file as `input_file`** — `run_catalog()` actually needs
  `catalog_*.csv` (picks are picked up automatically from the same directory). Passing the
  wrong input type is easy to miss because it doesn't crash, it just silently produces the wrong result.

### 7.5 Magnitude

- **Every ML is negative** → `a_coefficient = -1.0` (wrong sign). *Fix:* Hutton & Boore 1987
  `a=1.110` (POSITIVE), `c=0.00189`, `b=0.692`. Reset: delete `work/catalog/catalog_*_ml.csv`.
- **`KeyError: 'region'` when initializing `MLMagnitude`** → `self.reg = cfg["region"]` was
  *dead code* (ML computes hypocenter↔station distance, not from the region boundary).
  *Fix:* removed that line entirely — first confirmed via `grep -n "self\\.reg\\b"` that it
  wasn't used anywhere else.
- **`KeyError: 'station_file'`** after the fix above → `mag_cfg["data"]` didn't have that
  key yet (unlike `locate_cfg`/`assoc_cfg`). *Fix:* added `'station_file': str(sta_file)`.
- **Every ML = `NaN`** ("No ML computed") → a 6-column station file
  (`NET|STA|LOC(empty)|LAT|LON|ELEV`) was read with a fixed 5-column `usecols` → the empty
  LOC column shifted everything, so `lat` ended up wrong, making hypocenter–station distance
  ~20,004 km (antipodal!) for every station → all skipped (`dist_max_km` exceeded). *Fix:*
  `_load_stations()` now auto-detects the column count from the first line
  (`ncols = len(first_line.split("|"))`), mirroring the pattern already correct in
  `GammaAssociator._load_stations`.
- **ML slow (>17 min for 14 events, CPU ~100%)** — two inefficiencies in
  `MLMagnitude._find_waveform()`: (1) `glob.glob(..., recursive=True)` walks the
  **entire SDS archive** (~54,000 files) per lookup, up to 2× per event-station pair;
  (2) each pair does a separate file `read()`. *Fix:* for archives in standard SDS layout
  (`{year}/{net}/{sta}/{cha}.D/...`), use `obspy.clients.filesystem.sds.Client` —
  direct O(1) lookup with no tree walk, all 3 components at once without a separate
  `read()` (**~6× faster** per lookup, verified: glob 0.305s vs SDS client 0.048s).
  Automatic fallback to the old `glob` for non-SDS layouts (no behavior change there).
  Plus cross-event parallelization via `multiprocessing.Pool` (4 workers by default,
  `mag_cfg['magnitude']['workers']`, inventory loaded once per worker via
  `initializer` rather than per-event — important since a StationXML can be ~9 MB).

### 7.6 Web GUI

- **Recursive runner spawn** (`RUNNER_TEMPLATE` with no `__main__` guard) → a 778k-line log,
  the job stuck. *Fix:* the runner became a static file, `_pick_runner.py`/`_pipeline_runner.py`, with a guard.
- **Plotly blank white** → `cdn.plot.ly` unreachable from a LAN workstation. *Fix:* host
  `static/js/vendor/plotly-*.min.js` locally + lazy-load it.
- **Session leaking across configs** → listing now uses `activeConfigId`; `openWorkPage` sets
  `activeConfigId=cfgId`; status.json stores `cfg_id`.
- **Jobs orphaned after a restart** got mis-tagged `stopped` → now scans OS processes directly
  (`_live_runner_job_ids`, `active_jobs` in `/api/sysinfo`).
- **Auto-restart `os.execv` fails "Address already in use"** → the listening socket's FD was
  inherited. *Fix:* set `FD_CLOEXEC` on every fd before execv.
- **Waveform drum "flat"** → `max_amp` dominated by a single spike + DC offset. *Fix:* DEMEAN
  (`_wvMean`) + 99.5-percentile normalization (`_wvNorm`).
- **Waveform zoom "blocky"** → the backend read the whole day then sliced into bins. *Fix:* read
  ONLY the `[t0,t1]` window, send raw samples when `n≤max(px*4,12000)`.
- **Drum plot "blocky/checkered"** → `_renderDrum`'s per-bin `fillRect` + too few bins. *Fix:*
  px=`width*24` (~1 bin/pixel) + render an **ObsPy-dayplot**-style envelope (continuous
  polygon/line, color rotated per row `['#B2000F','#004C12','#847000','#0E01FF']`).
- **Drum "broken/dotted"** → separate min→max vertical lines per column. *Fix:* one continuous
  polyline (lineTo the maxes then the mins, connected across columns).
- **Result Viewer waveform "empty" = actually SLOW** → `picks_associated.csv`'s 3.5M rows
  were read on every click (~10s). *Fix:* cache a per-job picks index (`_RESULT_PICKS_CACHE`).
- **cfg_id isolation audit (2026-06-23)** — `cfg_id` in job listing endpoints
  (`/api/pipeline/jobs`, `/api/picking/jobs`, `/api/catalog/jobs`, `/api/pipeline/picks-files`,
  `/api/pipeline/catalog-files`, `/api/output/folders`) used to be OPTIONAL: if the frontend
  hadn't set `activeConfigId` yet (a race on initial load), the endpoint returned jobs from
  EVERY cfg_id. *Fix:* `cfg_id` is now required (400 if empty) on all those endpoints; the
  frontend got a `if (!activeConfigId) return;` guard before fetching. `filter-catalog` and
  `pipeline/run` (the `input_file` param) could also previously be given a `job_id`/path
  belonging to another cfg_id's job without an ownership check — *fix:* both now verify the
  source job's `status.json.cfg_id` matches the `cfg_id` sent, otherwise → 403.
- **Federation sync used the global token, not per-project** → `/api/sync/manifest` &
  `/api/sync/jobs/<id>/bundle` accepted the same server-wide `AUTH_TOKEN` as every other
  operator endpoint, so anyone who connected (a subscriber) could automatically read EVERY
  project on that server, not just the one they were meant to access. *Fix:* a new
  per-`cfg_id` token (`_cfg_sync_token`, stored in `gui_configs/<cfg_id>/.sync_token`,
  auto-generated via `secrets.token_urlsafe`); the `/api/sync/*` endpoints were pulled out
  of the global `AUTH_TOKEN` check and now require `?cfg_id=` + that token itself
  (`_check_sync_token`). Manage the token via `GET/POST /api/configs/<id>/sync-token`
  (localhost only, or whoever already holds the old token) or the CLI
  `seiswork remote sync-token --cfg ID [--regen]`.

### 7.7 Debugging techniques that proved useful
- Legacy Fortran SIGFPE/SIGSEGV → `gdb -batch -x cmds --args binary args` (run/bt full).
- A stuck GUI job → check `output.log` in `tmp/{picking,pipeline}/<job_id>/`, `status.json`,
  and the OS process (`ps aux | grep _pick_runner`).
- Picker not running from the GUI → make sure the runner is a static file with a `__main__` guard.

### 7.8 Full E2E simulation (1-day & 3-day) — cross-module bugs

Found while running the full end-to-end GUI pipeline (picking → assoc → magnitude →
relocation → imaging → mechanism) against real data; all fixed in code except the one
marked "third-party limitation".

- **`numpy` regression** — the `seiswork` env had been downgraded to `1.26.4` (an indirect
  pin from `skhash`/`pyrocko<2.0` during an earlier install), incompatible with the
  `xarray 2025.6.1` that Imaging needs → `ModuleNotFoundError: numpy.lib.array_utils`.
  *Fix:* `pip install --upgrade "numpy>=2.0"` (pyrocko still works despite the pip warning).
- **`AttributeError: 'phase'`** in the mechanism param-builder (`_pipeline_runner.py`) — GaMMA
  writes a `type` column, REAL writes a `phase` column; the code only handled one name.
  *Fix:* auto-detect `"phase" if "phase" in columns else "type"`.
- **`AttributeError: 'pick_time'`/`'event_id'`** — GaMMA uses the column names `timestamp`/
  `event_index`. *Fix:* normalize the column names + filter `event_index<0` (not associated)
  before computing polarity. The same normalization had to be applied to **both picks_p AND
  picks_s** — the first pass only covered picks_p, so FocoNet still crashed with `'pick_time'`
  on the picks_s path.
- **ObsPy `UTCDateTime` `TypeError`** couldn't parse a pandas string (`"...+00:00"` with a
  space) — this showed up in **two different places** (`mechanism/polarity.py` and
  `mechanism/sp_features.py`'s FocoNet path). *Fix:* a single `_to_utc()` helper:
  `pd.Timestamp(t).to_pydatetime()` first, then to `UTCDateTime`, used consistently everywhere.
- **`IndexError`** in SKHASH's own parser — an empty `$outfolder_plots` makes `value[0]`
  crash. *Fix:* omit the key from the control file when its value is empty (instead of writing blank).
- **`look_dep`/`nd0` parameters** were sent by the API but silently dropped — the param
  whitelist in `app.py` (mechanism builder) was too narrow. *Fix:* added passthrough for both.
- **Lists written as a Python repr** `"[0, 100, 10]"` instead of SKHASH's format
  (space-separated) → `" ".join(str(v) for v in val)`.
- **Imaging silently fell back to an old catalog** with no warning — `phase.dat` (the actual
  output of HypoDD catalog-mode) wasn't in the list of filenames `_find_hypodd_outputs`
  (`velocity/tomography.py`) searched for (`velesttohypo.pha`, `phase.pha`, `hypodd_phase.pha`).
  *Fix:* added `"phase.dat"` to the list — its format already matches exactly what the
  `Hypodd2simul2000` parser expects, just a different name. This bug only became visible
  AFTER the HypoDD input fix (see Section 7.4) made Imaging genuinely try to read the real
  relocation output for the first time.
- **Third-party limitation (not fixed)**: SKHASH hardcodes a depth lookup table capped at
  39 km, and has its own internal array-shape bug when parameters are extended manually —
  would need a patch to SKHASH's own source (outside SeisWork's code scope).
- Regression validation: every 1-day-simulation fix still works correctly on the 3-day
  simulation (3.5× more data volume); a GUI parameter audit on the 3-day simulation found
  no real issues (all 5 initial findings were false positives).
"""

DEBUG_ERRORS_ID = """\
## 7. Debug, Error & Solusi

Katalog masalah yang pernah ditemukan + akar penyebab + perbaikan (root-cause fix,
bukan workaround). Disusun per area.

### 7.1 Environment & CUDA

**cuDNN hang 30+ menit → `CUDNN_STATUS_NOT_INITIALIZED`.**
`nvidia-cudnn-cu13` (9.20) menimpa `cu12` (9.1.0); butuh CUDA 13 tapi driver hanya 12.2.
*Fix:*
```bash
pip uninstall nvidia-cudnn-cu13
pip install --force-reinstall nvidia-cudnn-cu12==9.1.0.70 \\
  nvidia-cublas-cu12==12.1.3.1 nvidia-cuda-nvrtc-cu12==12.1.105
```
Verifikasi `torch.backends.cudnn.version()` = 90100.

**Kernel Jupyter SIGSEGV saat picking paralel.**
`libmseed` tidak thread-safe — banyak thread `read()` paralel merusak state C global.
*Fix:* `_MSEED_LOCK = threading.Lock()` membungkus semua baca MSEED disk (FDSN aman).

**`libmkl symbol error` / iJIT.** *Fix:* `MKL_THREADING_LAYER=GNU` + `PYTHONNOUSERSITE=1`;
PyTorch via pip (bukan conda).

### 7.2 Picker

- **Pick terakhir di file hilang** → flush `if in_peak:` setelah loop.
- **`workers`/`batch_size` tak terpakai** → diteruskan ke `annotate()`.
- **Deteksi channel seisbench salah** → cek `"_P_" in ch` (bukan `"P" in ch`).
- **`phase_type` vs `phase_hint`** → seluruh pipeline membaca `phase_hint`.
- **`phase_amp` selalu NaN (PhaseNet/EQT)** → `_make_pick` tak punya akses waveform
  (hanya trace probabilitas). *Fix:* simpan `raw_stream` hidup sampai ekstraksi;
  `_measure_amp` ukur peak-to-peak/2 pada waveform FILTERED (P: Z, S: N/E).
- **`proc.wait()` hang state D** setelah picker selesai (cleanup MP). Picks/log sudah
  benar; status.json telat update. Mitigasi: timeout + fallback update dari picks.csv.
- **`io_processes` (multi-proses reader)**: GO (benar) tapi cuma ~6% lebih cepat —
  bottleneck HDD rotational `/dev/sda` (~44MB/s seek-thrash), bukan lock. Jangan
  harap speedup besar di disk ini.
- **`sds_lite` (pure-Python)**: correct (byte-identik) tapi **4.6–40× lebih lambat** dari
  `sds` (SDSClient sudah baca selektif di libmseed/C). NO-GO untuk bulk; hanya untuk
  jendela kecil/repick.

### 7.3 Asosiasi (GaMMA / REAL)

- **GaMMA lambat superlinear** → config tak set `use_dbscan`. *Fix:* `use_dbscan=True,
  dbscan_min_samples=3`, `dbscan_eps` dikosongkan (auto dari aperture÷vel_p).
  Speedup >×15 (terbukti A/B).
- **`pip install gamma` salah paket** (kristalografi) → pakai
  `pip install git+https://github.com/AI4EPS/GaMMA.git`.
- **Key wajib GaMMA**: `min_picks_per_eq` (bukan `min_picks_per_event`);
  `covariance_prior` harus **list** (`[5.0]`).
- **BGMM crash "Input X contains NaN"** → amplitudo NaN parsial. *Fix:* buang pick
  ber-NaN bila `use_amplitude=True` (cek `isna().any()`).
- **Magnitudo GaMMA over-estimate (mag≈8.0)** → `phase_amp` counts vs asumsi GaMMA m/s.
  *Fix:* konversi counts→m/s via sensitivitas inventory. Magnitudo otoritatif = ML WA.
- **REAL `FileNotFoundError`** → `os.makedirs(..., exist_ok=True)` untuk dir stasiun; juga
  ditemukan varian sama saat direktori LOG di-override oleh `_pipeline_runner.py` tapi
  tak pernah dibuat di `_run_one_day()`.
- **REAL `ValueError: too many values to unpack`** → tuple 4-elemen di-unpack sebagai 2
  (`for date, _ in ...`). *Fix:* `for date, *_ in ...`.
- **BGMM "hang" >1 jam** pada 7.357 picks bukan soal algoritma — param salah ketik
  (`use_amp` bukan `use_amplitude`, jadi `use_dbscan` tak pernah aktif). Setelah param
  benar + `use_dbscan=True`: 1 jam+ → **1,3 detik**.

### 7.4 Lokasi & relokasi (HypoDD / ph2dt / VELEST)

- **Path truncation getarg** (ph2dt/hypoDD pakai `character*30/*80`) → path absolut
  panjang ter-potong. *Fix:* panggil binary dengan `basename(inp)` + `cwd=work_dir`.
- **`_write_hypodd_inp` salah format** → getinp.f crash "End of file". *Fix:* rewrite
  template persis urutan getinp.f (9 filename → IDAT/IPHA/DIST → OBSCC/OBSCT → ... → VEL → CID).
- **SIGFPE div-by-zero di ph2dt.f** (katalog kecil → npair=0/nev=0). *Fix:* guard semua
  pembagian `if(divisor.gt.0)`, recompile. Pelajaran: SIGFPE Fortran sering multi-titik —
  pakai `gdb -batch` untuk lokasi persis.
- **"0 relocated events" bukan crash** (window pendek legitimate) → `_write_fallback_relocated`
  fallback ke katalog pra-relokasi (method `<orig>_no_reloc`).
- **`_load_station_df` pecah pada FDSN 6-kolom** (LOC kosong) → lat jadi NaN.
  *Fix:* auto-deteksi offset kolom lat via validasi range.
- **Katalog 1-hari TERLALU SPARSE** untuk differential-time (HypoDD/GrowClust/crosscorr
  gerak minim) — keterbatasan data, bukan bug. Match&Locate paling cocok (cari event baru).
- **NLLoc "0 events located"** padahal log `nlloc.log` bilang "14 events read" → kernel
  Jupyter masih meng-cache versi modul `NLLocLocator` lama (`grid_dir`/`model_prefix` dari
  `locate_cfg` diabaikan). *Fix:* **restart kernel** setelah edit modul SeisWork — edit
  `.py` tidak otomatis reload modul yang sudah ter-import.
- **Output `catalog_nlloc.csv`/`catalog_ml.csv` di-hardcode** ke `BASE/work/catalog/...`
  walau notebook/job punya direktori kerja sendiri → resiko `FileNotFoundError` atau baca
  hasil run sebelumnya yang usang. *Fix:* properti `catalog_dir` (alias `cat_dir`) di
  `NLLocLocator` & `MLMagnitude`, meniru konvensi yang sudah ada di `GammaAssociator`.
- **HypoDD dikasih file `.pha` sebagai `input_file`** — `run_catalog()` sebenarnya butuh
  `catalog_*.csv` (picks ikut otomatis dari direktori yang sama). Salah pakai jenis input
  ini gampang lolos karena tidak crash, hanya silently salah.

### 7.5 Magnitudo

- **Semua ML negatif** → `a_coefficient = -1.0` (tanda salah). *Fix:* Hutton & Boore 1987
  `a=1.110` (POSITIF), `c=0.00189`, `b=0.692`. Reset: hapus `work/catalog/catalog_*_ml.csv`.
- **`KeyError: 'region'` saat init `MLMagnitude`** → `self.reg = cfg["region"]` adalah
  *dead code* (ML hitung jarak hiposenter↔stasiun, bukan dari batas region). *Fix:* hapus
  baris itu sepenuhnya — konfirmasi dulu via `grep -n "self\\.reg\\b"` tak dipakai di tempat lain.
- **`KeyError: 'station_file'`** setelah fix di atas → `mag_cfg["data"]` belum punya key
  itu (beda dari `locate_cfg`/`assoc_cfg`). *Fix:* tambahkan `'station_file': str(sta_file)`.
- **Seluruh ML = `NaN`** ("No ML computed") → file stasiun 6-kolom
  (`NET|STA|LOC(kosong)|LAT|LON|ELEV`) terbaca dengan `usecols` 5-kolom tetap → kolom LOC
  kosong ter-geser jadi `lat`, jarak hiposenter–stasiun jadi ~20.004 km (antipodal!) untuk
  semua stasiun → ter-skip semua (`dist_max_km` terlampaui). *Fix:* `_load_stations()`
  deteksi otomatis jumlah kolom dari baris pertama (`ncols = len(first_line.split("|"))`),
  meniru pola yang sudah benar di `GammaAssociator._load_stations`.
- **ML lambat (>17 menit utk 14 event, CPU ~100%)** — dua sumber inefisiensi di
  `MLMagnitude._find_waveform()`: (1) `glob.glob(..., recursive=True)` menjelajahi
  **seluruh arsip SDS** (~54.000 file) per lookup, hingga 2× per pasangan event-stasiun;
  (2) tiap pasangan `read()` file terpisah. *Fix:* untuk arsip berformat SDS baku
  (`{tahun}/{net}/{sta}/{cha}.D/...`), pakai `obspy.clients.filesystem.sds.Client` —
  lookup langsung O(1) tanpa jelajah tree, 3 komponen sekaligus tanpa `read()` terpisah
  (**~6× lebih cepat** per lookup, terverifikasi: glob 0.305dtk vs SDS client 0.048dtk).
  Fallback otomatis ke `glob` lama untuk layout non-SDS (tak ada perubahan perilaku).
  Plus paralelisasi lintas-event via `multiprocessing.Pool` (4 worker default,
  `mag_cfg['magnitude']['workers']`, inventory dimuat sekali per worker lewat
  `initializer`, bukan per-event — penting karena StationXML bisa ~9 MB).

### 7.6 Web GUI

- **Runner spawn rekursi** (`RUNNER_TEMPLATE` tanpa guard `__main__`) → 778k baris log,
  job stuck. *Fix:* runner jadi file statis `_pick_runner.py`/`_pipeline_runner.py` dengan guard.
- **Plotly blank putih** → `cdn.plot.ly` tak terjangkau dari LAN workstation. *Fix:* host
  lokal `static/js/vendor/plotly-*.min.js` + lazy-load.
- **Sesi bocor antar-config** → listing pakai `activeConfigId`; `openWorkPage` set
  `activeConfigId=cfgId`; status.json simpan `cfg_id`.
- **Job orphaned pasca-restart** salah-tandai `stopped` → scan proses OS langsung
  (`_live_runner_job_ids`, `active_jobs` di `/api/sysinfo`).
- **Auto-restart `os.execv` gagal "Address already in use"** → FD socket listening
  diwarisi. *Fix:* set `FD_CLOEXEC` semua fd sebelum execv.
- **Waveform drum "flat"** → `max_amp` 1 spike + DC offset. *Fix:* DEMEAN (`_wvMean`) +
  norm persentil 99.5 (`_wvNorm`).
- **Waveform zoom "kotak"** → backend baca seluruh hari lalu slice bin. *Fix:* baca HANYA
  window `[t0,t1]`, kirim raw samples bila `n≤max(px*4,12000)`.
- **Drum plot "kotak-kotak"** → `_renderDrum` `fillRect` per-bin + bin kurang. *Fix:* px=
  `width*24` (~1 bin/pixel) + render envelope gaya **ObsPy dayplot** (poligon/garis kontinu,
  warna dirotasi per baris `['#B2000F','#004C12','#847000','#0E01FF']`).
- **Drum "putus-putus"** → garis vertikal min→max terpisah per kolom. *Fix:* satu polyline
  kontinu (lineTo maxs lalu mins, tersambung antar kolom).
- **Result Viewer waveform "empty" = LAMBAT** → `picks_associated.csv` 3.5jt baris dibaca
  tiap klik (~10s). *Fix:* cache index picks per-job (`_RESULT_PICKS_CACHE`).
- **Audit isolasi antar-cfg_id (2026-06-23)** — `cfg_id` di listing job
  (`/api/pipeline/jobs`, `/api/picking/jobs`, `/api/catalog/jobs`, `/api/pipeline/picks-files`,
  `/api/pipeline/catalog-files`, `/api/output/folders`) tadinya OPSIONAL: jika frontend
  belum sempat set `activeConfigId` (race saat load awal), endpoint balik job dari SEMUA
  cfg_id. *Fix:* `cfg_id` kini wajib (400 jika kosong) di semua endpoint itu; frontend diberi
  guard `if (!activeConfigId) return;` sebelum fetch. `filter-catalog` dan `pipeline/run`
  (param `input_file`) juga tadinya bisa dikasih `job_id`/path job milik cfg_id lain tanpa
  validasi ownership — *fix:* keduanya sekarang cek `status.json.cfg_id` job sumber harus
  sama dengan `cfg_id` yang dikirim, kalau tidak → 403.
- **Federation sync pakai token global, bukan per-project** → `/api/sync/manifest` &
  `/api/sync/jobs/<id>/bundle` menerima token server-wide `AUTH_TOKEN` yang sama dengan
  endpoint operator lain, sehingga siapapun yang terhubung (subscriber) otomatis bisa
  membaca SEMUA project di server itu, bukan hanya project yang seharusnya dia akses.
  *Fix:* token baru per-`cfg_id` (`_cfg_sync_token`, file `gui_configs/<cfg_id>/.sync_token`,
  auto-generate `secrets.token_urlsafe`), endpoint `/api/sync/*` dikeluarkan dari pengecekan
  `AUTH_TOKEN` global dan wajib `?cfg_id=` + token itu sendiri (`_check_sync_token`). Lihat
  pengaturan token: `GET/POST /api/configs/<id>/sync-token` (hanya dari localhost atau yang
  sudah pegang token lama) atau CLI `seiswork remote sync-token --cfg ID [--regen]`.

### 7.7 Cara debug yang terbukti berguna
- SIGFPE/SIGSEGV Fortran legacy → `gdb -batch -x cmds --args binary args` (run/bt full).
- Job GUI stuck → cek `output.log` di `tmp/{picking,pipeline}/<job_id>/`, `status.json`,
  dan proses OS (`ps aux | grep _pick_runner`).
- Picker tak jalan dari GUI → pastikan runner file statis + guard `__main__`.

### 7.8 Simulasi E2E penuh (1 hari & 3 hari) — bug lintas-modul

Ditemukan saat menjalankan pipeline GUI penuh end-to-end (picking → assoc → magnitude →
relokasi → imaging → mechanism) dengan data nyata; semua sudah diperbaiki di kode kecuali
yang ditandai "limitasi pihak ketiga".

- **Regresi `numpy`** — env `seiswork` ter-downgrade ke `1.26.4` (pin tak langsung dari
  `skhash`/`pyrocko<2.0` saat instalasi sebelumnya), tak kompatibel dengan `xarray
  2025.6.1` yang dibutuhkan Imaging → `ModuleNotFoundError: numpy.lib.array_utils`.
  *Fix:* `pip install --upgrade "numpy>=2.0"` (pyrocko tetap berfungsi meski pip warning).
- **`AttributeError: 'phase'`** di mechanism param-builder (`_pipeline_runner.py`) — GaMMA
  menulis kolom `type`, REAL menulis kolom `phase`; kode cuma menangani satu nama.
  *Fix:* deteksi otomatis `"phase" if "phase" in columns else "type"`.
- **`AttributeError: 'pick_time'`/`'event_id'`** — GaMMA pakai nama kolom `timestamp`/
  `event_index`. *Fix:* normalisasi nama kolom + filter `event_index<0` (tak terasosiasi)
  sebelum polaritas dihitung. Normalisasi sama harus diterapkan ke **picks_p MAUPUN
  picks_s** — versi awal cuma kena picks_p, FocoNet tetap crash `'pick_time'` di jalur picks_s.
- **`TypeError` ObsPy `UTCDateTime`** tak bisa parse string pandas (`"...+00:00"` dengan
  spasi) — muncul di **dua tempat berbeda** (`mechanism/polarity.py` dan
  `mechanism/sp_features.py` jalur FocoNet). *Fix:* helper `_to_utc()` tunggal:
  `pd.Timestamp(t).to_pydatetime()` dulu sebelum ke `UTCDateTime`, dipakai konsisten di semua pemanggilan.
- **`IndexError`** di parser SKHASH sendiri — `$outfolder_plots` kosong bikin `value[0]`
  crash. *Fix:* omit key dari control file bila value kosong (bukan tulis blank).
- **Parameter `look_dep`/`nd0`** dikirim API tapi di-drop diam-diam — whitelist param di
  `app.py` (mechanism builder) terlalu sempit. *Fix:* tambahkan passthrough keduanya.
- **List ditulis sebagai Python repr** `"[0, 100, 10]"` bukan format SKHASH
  (space-separated) → `" ".join(str(v) for v in val)`.
- **Imaging diam-diam fallback ke katalog lama** tanpa peringatan — `phase.dat` (output
  asli HypoDD catalog-mode) tidak ada di daftar nama yang dicari
  (`velesttohypo.pha`, `phase.pha`, `hypodd_phase.pha`) di `_find_hypodd_outputs`
  (`velocity/tomography.py`). *Fix:* tambahkan `"phase.dat"` ke daftar — formatnya sudah
  identik dengan yang diharapkan parser `Hypodd2simul2000`, hanya beda nama. Bug ini baru
  kelihatan SETELAH fix input HypoDD (lihat Bagian 7.4) membuat Imaging untuk pertama kalinya
  benar-benar mencoba baca output relokasi asli.
- **Limitasi pihak ketiga (tidak diperbaiki)**: SKHASH hardcode lookup-table kedalaman
  maks 39 km, dan punya bug array-shape internal sendiri saat parameter diperluas manual —
  perlu patch ke source SKHASH (di luar scope kode SeisWork).
- Validasi regresi: seluruh fix simulasi 1-hari tetap bekerja benar pada simulasi 3-hari
  (volume data 3,5× lebih besar); audit parameter GUI pada simulasi 3-hari tidak menemukan
  masalah nyata (5 temuan awal semua false positive).
"""



def _sig(node):
    args = [a.arg for a in node.args.args]
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    return "(" + ", ".join(args) + ")"


def _first_line(doc, lang="en"):
    if not doc:
        return "_(no docstring)_" if lang == "en" else "_(tanpa docstring)_"
    line = doc.strip().split("\n")[0].strip()
    return line


def _source_of(src_lines, node):
    """Full source text of a function/method node, decorators included.
    AST line numbers are 1-based; decorator_list (if any) starts earlier
    than node.lineno."""
    start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
    end = node.end_lineno
    return "\n".join(src_lines[start - 1:end])


def _fn_details(node, src_lines) -> str:
    """One collapsible <details> block: signature + first docstring line in
    the always-visible <summary>, full source revealed on click. Raw HTML
    (not a markdown table) so it survives the md->HTML pass untouched; the
    `markdown` package passes well-formed HTML blocks through as-is."""
    import html as _html
    sig = f"{node.name}{_sig(node)}"
    doc = ast.get_docstring(node)
    # _fn_details renders raw HTML, not markdown; _first_line()'s "_..._" italic
    # markup (its no-docstring sentinel) would otherwise show as literal underscores.
    desc = _first_line(doc) if doc else "(no docstring)"
    code = _html.escape(_source_of(src_lines, node))
    return (
        f'<details class="fn-src">'
        f'<summary><code>{_html.escape(sig)}</code>'
        f'<span class="fn-desc">{_html.escape(desc)}</span></summary>'
        f'<pre><code>{code}</code></pre>'
        f'</details>'
    )


def build_api_reference(lang="en"):
    """Extract the per-module function/class reference via AST. Each
    function/method becomes a clickable <details> block revealing its
    full source."""
    MODULE_DESC = MODULE_DESC_EN if lang == "en" else MODULE_DESC_ID
    if lang == "en":
        out = ["## 6. Function Reference (per module)\n",
               "_Auto-extracted from source — click any function/method to see its "
               "full source code. Leading `_underscore_` = internal function._\n"]
    else:
        out = ["## 6. Referensi Fungsi (per modul)\n",
               "_Diekstrak otomatis dari source — klik tiap fungsi/method untuk melihat "
               "source code lengkapnya. Garis bawah `_underscore_` = fungsi internal._\n"]
    n_func = n_cls = n_meth = 0
    for py in sorted(PKG.rglob("*.py")):
        if "__pycache__" in str(py):
            continue
        rel = str(py.relative_to(BASE))
        text = py.read_text()
        src_lines = text.splitlines()
        try:
            tree = ast.parse(text, filename=rel)
        except Exception as e:
            out.append(f"### `{rel}`\n\n> ⚠️ parse error: {e}\n")
            continue
        funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        if not funcs and not classes and rel not in MODULE_DESC:
            continue
        out.append(f"### `{rel}`\n")
        desc = MODULE_DESC.get(rel)
        if desc:
            out.append(f"{desc}\n")
        moddoc = ast.get_docstring(tree)
        if moddoc:
            out.append(f"> {_first_line(moddoc, lang)}\n")
        for cls in classes:
            n_cls += 1
            out.append(f"\n**class `{cls.name}`** — {_first_line(ast.get_docstring(cls), lang)}\n")
            meths = [s for s in cls.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for m in meths:
                n_meth += 1
                out.append("")   # blank line: markdown must see each <details> as its own
                out.append(_fn_details(m, src_lines))  # raw-HTML block, else it tries to
            out.append("")                             # markdown-parse the source code
        if funcs:
            for f in funcs:
                n_func += 1
                out.append("")
                out.append(_fn_details(f, src_lines))
            out.append("")
    return "\n".join(out), (n_func, n_cls, n_meth)


def _generate_diagrams():
    """Run the mindmap generator; return a dict of PNG paths."""
    paths = {}
    for name, module, func in [
        ("mindmap",  "generate_mindmap",  "generate_mindmap"),
    ]:
        try:
            import importlib
            mod = importlib.import_module(module)
            out_path = getattr(mod, func)(DOCS)
            paths[name] = Path(out_path)
            print(f"✓ {name}: {out_path}")
        except Exception as e:
            print(f"  ✗ {name} gagal: {e}")
    return paths


def _header_banner_html() -> str:
    """Centered sub-header banner using the same og-preview.png as the
    native-GUI splash screen and the site's Open Graph image, embedded as
    base64 so it needs no network round-trip (one asset, consistent branding)."""
    png = PKG / "web" / "static" / "img" / "og-preview.png"
    try:
        b64 = base64.b64encode(png.read_bytes()).decode("ascii")
        return (f'<div style="text-align:center;margin:4px 0 16px">'
                f'<img src="data:image/png;base64,{b64}" alt="SeisWork" '
                f'style="max-width:480px;width:100%;height:auto;border-radius:10px" /></div>')
    except Exception:
        return ""


def _author_block_html(lang: str) -> str:
    """Author/affiliation/ORCID/Scopus block, placed at the bottom of the
    intro (after description + code-coverage line, before the TOC) and
    bolded as one unit. Raw HTML (not markdown) so the inline ORCID SVG and
    bold-everything styling stay reliable; markdown lists inside a bold
    wrapper are fragile."""
    orcid_svg = ('<svg width="15" height="15" viewBox="0 0 16 16" '
                 'style="vertical-align:-3px;margin-right:3px" xmlns="http://www.w3.org/2000/svg">'
                 '<circle cx="8" cy="8" r="8" fill="#A6CE39"/>'
                 '<text x="8" y="11.3" font-family="Arial,sans-serif" font-size="8.2" '
                 'font-weight="700" fill="#fff" text-anchor="middle">iD</text></svg>')
    affil_label = "Affiliations" if lang == "en" else "Afiliasi"
    return f"""
<div class="doc-author-block">
<p><strong>Author:</strong> HakimBMKG (arif.hakim@bmkg.go.id, arif_rachman_hakim@mail.iggcas.ac.cn)</p>
<p><strong>{affil_label}:</strong></p>
<ol>
<li>Institute of Geology and Geophysics, Chinese Academy of Sciences — Beijing, Beijing, CN</li>
<li>University of Chinese Academy of Sciences — Beijing, Beijing, CN</li>
<li>Meteorological, Climatological, and Geophysical Agency — Jakarta, ID</li>
</ol>
<p><strong>ORCID:</strong> {orcid_svg}<a href="https://orcid.org/0000-0002-4955-5432">0000-0002-4955-5432</a> · <strong>Scopus Author ID:</strong> 57329132400</p>
<p><strong>Created:</strong> {CREATED_DATE}</p>
</div>
"""


def _img_tag(png_path: Path, caption: str, max_width: str = "100%",
             max_height: str = "22cm") -> str:
    """Embed a PNG as a base64 HTML <figure>.

    A tall diagram like the mindmap (vertical tree, ~1:3 ratio) would become
    huge if only max-width applied: width shrinks but height stays
    proportional, so it spills across many pages. max-height also scales
    down by height, so the image always fits on one A4 page.
    """
    try:
        b64 = base64.b64encode(png_path.read_bytes()).decode()
        return (
            f'<figure style="text-align:center;margin:18px 0;'
            f'page-break-inside:avoid;">'
            f'<img src="data:image/png;base64,{b64}" '
            f'style="max-width:{max_width};max-height:{max_height};'
            f'width:auto;height:auto;'
            f'border:1px solid #d4dcea;border-radius:6px;" />'
            f'<figcaption style="font-size:8.5pt;color:#555;margin-top:4px;">'
            f'{caption}</figcaption></figure>'
        )
    except Exception as e:
        return f'<p><em>[Gambar tidak tersedia: {e}]</em></p>'


def _out_paths(lang: str):
    suffix = "" if lang == "en" else f".{lang}"
    d = BASE / "docs"
    return (d / f"SeisWork_Dokumentasi_Lengkap{suffix}.md",
            d / f"SeisWork_Dokumentasi_Lengkap{suffix}.html",
            d / f"SeisWork_Dokumentasi_Lengkap{suffix}.pdf")


_MOD_GROUPS_EN = [
    ("Core & CLI", ["seiswork/__init__.py", "seiswork/cli.py", "seiswork/client.py", "seiswork/parallel.py"]),
    ("Picker", "/picker/"), ("Associator", "/associator/"), ("Velocity", "/velocity/"),
    ("Locator", "/locator/"), ("Magnitude", "/magnitude/"),
    ("Relocation & Detection", ("/relocation/", "/detection/")),
    ("Preparation", "/preparation/"), ("Utils", "/utils/"), ("Web GUI", "/web/"),
]
_MOD_GROUPS_ID = [
    ("Inti & CLI", ["seiswork/__init__.py", "seiswork/cli.py", "seiswork/client.py", "seiswork/parallel.py"]),
    ("Picker", "/picker/"), ("Associator", "/associator/"), ("Velocity", "/velocity/"),
    ("Locator", "/locator/"), ("Magnitude", "/magnitude/"),
    ("Relocation & Detection", ("/relocation/", "/detection/")),
    ("Preparation", "/preparation/"), ("Utils", "/utils/"), ("Web GUI", "/web/"),
]


def _references_md(lang: str) -> str:
    """Read the References table straight out of README.md, the single
    source of truth, so the ~25 hand-verified citations never drift out of
    sync with a second pasted-in copy."""
    heading = "## 8. References" if lang == "en" else "## 8. Referensi"
    intro = ("Method/algorithm used at each pipeline stage, from phase picking through "
              "tomographic imaging and focal mechanisms. Every DOI was verified against "
              "Crossref/the publisher." if lang == "en" else
              "Metode/algoritma yang dipakai di tiap tahap pipeline, dari phase picking "
              "hingga tomografi dan mekanisme fokal. Setiap DOI telah diverifikasi "
              "terhadap Crossref/penerbit.")
    try:
        text = (BASE / "README.md").read_text(encoding="utf-8")
        body = text[text.index("## References"):].strip()
        lines = body.split("\n")
        table_start = next(i for i, l in enumerate(lines) if l.startswith("| Stage"))
        table = "\n".join(lines[table_start:])
    except Exception as e:
        return f"{heading}\n\n> ⚠️ Could not load README.md references: {e}\n"
    return f"{heading}\n\n{intro}\n\n{table}\n"


def _build_doc(lang: str, diagram_paths: dict):
    OUT, OUT_HTML, OUT_PDF = _out_paths(lang)
    MODULE_DESC = MODULE_DESC_EN if lang == "en" else MODULE_DESC_ID
    api_md, (nf, nc, nm) = build_api_reference(lang)

    if lang == "en":
        header = f"""\
# SeisWork — Full Documentation

<p class="doc-version-tag">Version {SW_VERSION}</p>

{_header_banner_html()}

**Simple Seismological Data Processing Framework**

This document combines: overview & architecture, system requirements, installation,
usage (CLI + Web GUI), per-module/function descriptions, and a debug & error catalog.

> Code coverage: **{len(MODULE_DESC)} documented modules**, **{nf} functions**,
> **{nc} classes**, **{nm} methods** (function reference auto-extracted — Section 6).

{_author_block_html("en")}

---

### Table of Contents
1. Overview & Architecture
2. System Requirements
3. Installation
4. Usage (CLI + Web GUI)
5. Module Description
6. Function Reference (per module)
7. Debug, Errors & Fixes
8. References

---
"""
        mod_section = ["## 5. Module Description\n"]
        groups, footer = _MOD_GROUPS_EN, "\n---\n\n_© HakimBMKG, BMKG._\n"
        sections = [OVERVIEW_EN, REQUIREMENTS_EN, INSTALL_EN, USAGE_EN]
        tail = DEBUG_ERRORS_EN
    else:
        header = f"""\
# SeisWork — Dokumentasi Lengkap

<p class="doc-version-tag">Versi {SW_VERSION}</p>

{_header_banner_html()}

**Framework Sederhana Pemrosesan Data Seismologi**

Dokumen ini menggabungkan: ikhtisar & arsitektur, kebutuhan sistem, instalasi,
penggunaan (CLI + Web GUI), deskripsi tiap modul/fungsi, serta katalog debug & error.

> Cakupan kode: **{len(MODULE_DESC)} modul terdokumentasi**, **{nf} fungsi**,
> **{nc} kelas**, **{nm} method** (referensi fungsi diekstrak otomatis — Bagian 6).

{_author_block_html("id")}

---

### Daftar Isi
1. Ikhtisar & Arsitektur
2. Kebutuhan Sistem (Requirements)
3. Instalasi
4. Penggunaan (CLI + Web GUI)
5. Deskripsi Modul
6. Referensi Fungsi (per modul)
7. Debug, Error & Solusi
8. Referensi

---
"""
        mod_section = ["## 5. Deskripsi Modul\n"]
        groups, footer = _MOD_GROUPS_ID, "\n---\n\n_© HakimBMKG, BMKG._\n"
        sections = [OVERVIEW_ID, REQUIREMENTS_ID, INSTALL_ID, USAGE_ID]
        tail = DEBUG_ERRORS_ID

    for gname, sel in groups:
        if isinstance(sel, list):
            keys = sel
        elif isinstance(sel, tuple):
            keys = [k for k in MODULE_DESC if any(s in k for s in sel)]
        else:
            keys = [k for k in MODULE_DESC if sel in k]
        if not keys:
            continue
        mod_section.append(f"### {gname}\n")
        for k in keys:
            mod_section.append(f"- **`{k}`** — {MODULE_DESC[k]}")
        mod_section.append("")
    mod_md = "\n".join(mod_section)

    full = "\n".join([header, *sections, mod_md, api_md, tail, _references_md(lang), footer])
    OUT.write_text(full, encoding="utf-8")
    print(f"✓ Markdown ({lang}): {OUT}  ({len(full):,} char, {full.count(chr(10))} lines)")
    print(f"  Reference: {nf} functions, {nc} classes, {nm} methods from {len(MODULE_DESC)} modules.")
    _export_pdf(full, lang, diagram_paths)


def main():
    diagram_paths = _generate_diagrams()
    for lang in ("en", "id"):
        _build_doc(lang, diagram_paths)


def _export_pdf(md_text, lang="en", diagram_paths=None):
    """Convert Markdown -> styled HTML -> PDF (weasyprint). PDF is always
    built for documentation commands."""
    OUT, OUT_HTML, OUT_PDF = _out_paths(lang)
    try:
        import markdown as _md
    except Exception as e:
        print(f"  ✗ 'markdown' module missing ({e}) — skipping HTML/PDF. "
              f"Install: pip install markdown")
        return
    body = _md.markdown(
        md_text,
        extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
    )

    # Inject diagram images after the <!-- INJECT_DIAGRAMS --> placeholder
    if diagram_paths:
        diag_html = '<div>\n'
        diag_html += ('<h3>Architecture Diagram</h3>\n' if lang == "en"
                      else '<h3>Diagram Arsitektur</h3>\n')
        if "mindmap" in diagram_paths:
            caption = (f"Figure 1 — SeisWork Component Mindmap {SW_VERSION}" if lang == "en"
                       else f"Gambar 1 — Mindmap Komponen SeisWork {SW_VERSION}")
            diag_html += _img_tag(diagram_paths["mindmap"], caption, max_width="90%")
        diag_html += '</div>\n'
        body = body.replace("<!-- INJECT_DIAGRAMS -->", diag_html)

    title = "SeisWork — Full Documentation" if lang == "en" else "SeisWork — Dokumentasi Lengkap"
    html = (f"<!DOCTYPE html><html lang='{lang}'><head><meta charset='utf-8'>"
            f"<title>{title}</title>"
            f"<style>{PDF_CSS}{LOADING_CSS}{SEARCH_CSS}</style></head>"
            f"<body>{_loading_html(lang)}{_search_bar_html(lang)}{body}{_search_js(lang)}</body></html>")
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"✓ HTML ({lang}): {OUT_HTML}")
    try:
        import weasyprint
        weasyprint.HTML(string=html, base_url=str(BASE)).write_pdf(str(OUT_PDF))
        print(f"✓ PDF  ({lang}): {OUT_PDF}")
    except Exception as e:
        print(f"  ✗ weasyprint failed ({e}) — skipping PDF. "
              f"Install in the seiswork env: pip install weasyprint")


if __name__ == "__main__":
    main()

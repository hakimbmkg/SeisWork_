#!/usr/bin/env python3
"""
SeisWork - Mindmap Generator v2
Author : HakimBMKG

Generates an accurate SeisWork framework mindmap reflecting the actual
code structure and implementation status of each component.

Output: docs/seiswork_mindmap.png
        docs/seiswork_mindmap.pdf

Run:
    conda activate seiswork
    python docs/generate_mindmap.py
"""

import sys
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import matplotlib.patheffects as pe
import numpy as np

try:
    from seiswork import __version__ as _SW_VERSION
except Exception:
    _SW_VERSION = "?"

# ─────────────────────────────────────────────────────────────────────────────
#  Mindmap tree definition
#  status: "ok"=implemented, "manual"=requires manual install, "plan"=planned
# ─────────────────────────────────────────────────────────────────────────────

TREE = {
    "label": f"seiswork\n{_SW_VERSION}",
    "color": "#1a1a2e",
    "text_color": "white",
    "bold": True,
    "children": [
        # ── Preparation ───────────────────────────────────────────────────────
        {
            "label": "Preparation",
            "color": "#f0a500",
            "text_color": "white",
            "children": [
                {"label": "DownloadStation",   "status": "ok",   "desc": "FDSN → inventory.xml\nstations.txt"},
                {"label": "DownloadWaveforms", "status": "ok",   "desc": "FDSN → SDS archive\nper station-day"},
                {"label": "ImportCatalog",     "status": "ok",   "desc": "ISC / SCML / QuakeML\n→ catalog_reference.csv"},
                {"label": "MapStation",        "status": "plan", "desc": "cartopy map\nstasiun + region"},
                {"label": "CheckWaveforms",    "status": "plan", "desc": "plot coverage\nSDS availability"},
            ],
        },
        # ── Picker ────────────────────────────────────────────────────────────
        {
            "label": "Picker",
            "color": "#4361ee",
            "text_color": "white",
            "children": [
                {"label": "PhaseNet",          "status": "ok",   "desc": "seisbench/PyTorch GPU\nfile|sds|fdsn|sds+fdsn"},
                {"label": "EQTransformer",     "status": "ok",   "desc": "via seisbench\n(model=EQTransformer)"},
                {"label": "STALTA",            "status": "ok",   "desc": "ObsPy recursive\nP-picks only"},
                {"label": "PhaseNet Native",   "status": "ok",   "desc": "TF predict.py Docker\nfname.csv glob SDS"},
                {"label": "CrossCorrelation",  "status": "plan", "desc": "waveform CC picks\ntemplate-based"},
                {"label": "TemplateMatching",  "status": "plan", "desc": "matched filter\ndetection"},
            ],
        },
        # ── Associator ────────────────────────────────────────────────────────
        {
            "label": "Associator",
            "color": "#7b2d8b",
            "text_color": "white",
            "children": [
                {"label": "GaMMA",     "status": "ok",   "desc": "Bayesian GMM\nZhu et al. 2022"},
                {"label": "REAL",      "status": "ok",   "desc": "grid-search\nDal-mzhang/REAL"},
                {"label": "PhaseLink", "status": "plan", "desc": "graph-based\nassociation"},
                {"label": "PyOCTO",    "status": "plan", "desc": "OctoTree\nassociation"},
                {"label": "GNN",       "status": "plan", "desc": "Graph Neural Net\nPhaseAssoc"},
            ],
        },
        # ── Locator ───────────────────────────────────────────────────────────
        {
            "label": "Locator",
            "color": "#3a86ff",
            "text_color": "white",
            "children": [
                {"label": "NLLoc",       "status": "ok",     "desc": "NonLinLoc probabilistik\n3D grid search"},
                {"label": "Hypoinverse", "status": "ok",     "desc": "iterative least-sq\nKlein 2002"},
                {"label": "LocSAT",      "status": "ok",     "desc": "via SeisComP\nscautoloc/scolv"},
                {"label": "Hypo71",      "status": "plan",   "desc": "klasik USGS\nPython wrapper"},
                {"label": "SCAUTOLOC",   "status": "plan",   "desc": "SeisComP auto\nlocation module"},
            ],
        },
        # ── Velocity ──────────────────────────────────────────────────────────
        {
            "label": "Velocity",
            "color": "#2dc653",
            "text_color": "white",
            "children": [
                {"label": "VELEST",   "status": "ok",   "desc": "1D local P-model\nKissling et al. 1994"},
                {"label": "SIMUL2000","status": "ok",   "desc": "3D LET tomography\nEvans et al. 1994"},
                {"label": "TomoDD",   "status": "plan", "desc": "3D DD tomografi\nZhang & Thurber 2003"},
            ],
        },
        # ── Magnitude ─────────────────────────────────────────────────────────
        {
            "label": "Magnitude",
            "color": "#e63946",
            "text_color": "white",
            "children": [
                {"label": "ML",     "status": "ok",   "desc": "Wood-Anderson ObsPy\nRichter 1935"},
                {"label": "Mb",     "status": "plan", "desc": "teleseismic body\nmagnitude"},
                {"label": "MagNet", "status": "plan", "desc": "deep learning\nmagnitude"},
            ],
        },
        # ── Relocation ────────────────────────────────────────────────────────
        {
            "label": "Relocation",
            "color": "#c77dff",
            "text_color": "white",
            "children": [
                {"label": "HypoDD",   "status": "ok",   "desc": "double-difference\ncatalog + CC mode"},
                {"label": "GrowClust","status": "ok",   "desc": "waveform CC\nTrugman & Shearer 2017"},
                {"label": "FDTCC",    "status": "ok",   "desc": "differential travel-time\ndt.cc + dt.ct"},
                {"label": "scrtdd",   "status": "plan", "desc": "real-time DD\nSeisComP plugin"},
            ],
        },
        # ── Detection ─────────────────────────────────────────────────────────
        {
            "label": "Detection",
            "color": "#ff6b35",
            "text_color": "white",
            "children": [
                {"label": "Match&Locate", "status": "ok",   "desc": "MatchLocate2 + SelectFinal\nself-detect CC=1.0000"},
                {"label": "FDTCC-refine", "status": "ok",   "desc": "GrowClust-refine\ntheoretical-phase 50/50"},
            ],
        },
        # ── CrossCorr ─────────────────────────────────────────────────────────
        {
            "label": "CrossCorr\nAnalysis",
            "color": "#e9c46a",
            "text_color": "#1a1a2e",
            "children": [
                {"label": "CC Statistik", "status": "ok",   "desc": "CC distribution\n3-tab modal web"},
                {"label": "CC Waveform",  "status": "ok",   "desc": "trace overlay\nwaveform viewer"},
                {"label": "CC RMS",       "status": "ok",   "desc": "RMS annotation\nper event"},
            ],
        },
        # ── Noise ─────────────────────────────────────────────────────────────
        {
            "label": "Noise",
            "color": "#f4a261",
            "text_color": "white",
            "children": [
                {"label": "NoisePy ASDF",  "status": "ok",   "desc": "native noisepy.seis\nSDS → ASDF archive"},
                {"label": "CCF / Stack",   "status": "plan", "desc": "cross-correlation\n+ stacking (next)"},
                {"label": "PSD",           "status": "plan", "desc": "Power Spectral Density\nObsPy PPSD"},
                {"label": "ANT",           "status": "plan", "desc": "Ambient Noise\nTomography"},
            ],
        },
        # ── Mechanism ─────────────────────────────────────────────────────────
        {
            "label": "Mechanism",
            "color": "#ef476f",
            "text_color": "white",
            "children": [
                {"label": "SKHASH/HASH", "status": "ok", "desc": "focal mechanism\nfrom raw-waveform polarity"},
                {"label": "FocoNet",     "status": "ok", "desc": "transformer DL\nSong et al. 2026"},
                {"label": "Beachball 2D/3D", "status": "ok", "desc": "Leaflet + Plotly\ncombined map"},
            ],
        },
        # ── Imaging ───────────────────────────────────────────────────────────
        {
            "label": "Imaging",
            "color": "#06d6a0",
            "text_color": "#1a1a2e",
            "children": [
                {"label": "SIMUL2000 LET", "status": "ok", "desc": "3D Vp tomography\n(local earthquake)"},
                {"label": "3D Vp Viewer",  "status": "ok", "desc": "interactive Plotly\nvolume viewer"},
            ],
        },
        # ── Slab2 ─────────────────────────────────────────────────────────────
        {
            "label": "Slab2",
            "color": "#ffd166",
            "text_color": "#1a1a2e",
            "children": [
                {"label": "Auto slab per-AOI", "status": "ok", "desc": "USGS Slab2.0\nregion-agnostic"},
                {"label": "2D/3D contour",      "status": "ok", "desc": "GeoJSON feed\nmap + 3D viewer"},
            ],
        },
        # ── Online Monitor ────────────────────────────────────────────────────
        {
            "label": "Online\nMonitor",
            "color": "#073b4c",
            "text_color": "white",
            "children": [
                {"label": "Real-time picking", "status": "ok", "desc": "seedlink + scautopick\nlive waveform"},
                {"label": "Online Viewer",      "status": "ok", "desc": "read-only mirror\nper-config URL"},
            ],
        },
        # ── Federation ────────────────────────────────────────────────────────
        {
            "label": "Federation",
            "color": "#118ab2",
            "text_color": "white",
            "children": [
                {"label": "REST API + token", "status": "ok", "desc": "server ⇄ client\nbearer-token auth"},
                {"label": "remote CLI",        "status": "ok", "desc": "seiswork remote\nsync per cfg_id"},
            ],
        },
        # ── Presentation ──────────────────────────────────────────────────────
        {
            "label": "Presentation",
            "color": "#8338ec",
            "text_color": "white",
            "children": [
                {"label": "Slide builder", "status": "ok", "desc": "map2D/3D, histogram,\nstat table, split"},
            ],
        },
        # ── IO / Converter ────────────────────────────────────────────────────
        {
            "label": "IO / Converter",
            "color": "#457b9d",
            "text_color": "white",
            "children": [
                {"label": "picks → VELEST",      "status": "ok", "desc": ".pha format"},
                {"label": "picks → NLLoc",       "status": "ok", "desc": "NLLOC_OBS format"},
                {"label": "picks → HypoDD",      "status": "ok", "desc": "phase.dat"},
                {"label": "picks → Hypoinverse", "status": "ok", "desc": "ARC format"},
                {"label": "picks → SCML",        "status": "ok", "desc": "SeisComP XML"},
            ],
        },
        # ── Pipeline ──────────────────────────────────────────────────────────
        {
            "label": "Pipeline",
            "color": "#1d3557",
            "text_color": "white",
            "children": [
                {"label": "run_parallel", "status": "ok",   "desc": "chunked orchestrator\nspawn ProcessPool"},
                {"label": "Plotter",      "status": "ok",   "desc": "cartopy maps\ndepth sections"},
                {"label": "Report",       "status": "plan", "desc": "HTML/PDF summary\nauto-generated"},
            ],
        },
        # ── bin ───────────────────────────────────────────────────────────────
        {
            "label": "bin\n(compiled)",
            "color": "#6c757d",
            "text_color": "white",
            "children": [
                {"label": "NLLoc",       "status": "ok",     "desc": "NonLinLoc (C)"},
                {"label": "Grid2Time",   "status": "ok",     "desc": "NLLoc travel-time"},
                {"label": "Vel2Grid",    "status": "ok",     "desc": "NLLoc vel grid"},
                {"label": "hypoDD",      "status": "ok",     "desc": "HypoDD (Fortran)"},
                {"label": "ph2dt",       "status": "ok",     "desc": "diff-time prep"},
                {"label": "growclust",   "status": "ok",     "desc": "GrowClust (Fortran)"},
                {"label": "REAL",        "status": "ok",     "desc": "assoc binary (C)"},
                {"label": "velest",      "status": "manual", "desc": "ETHZ license (manual)"},
                {"label": "hypoinverse", "status": "manual", "desc": "USGS license (manual)"},
            ],
        },
    ],
}

# ─────────────────────────────────────────────────────────────────────────────
#  Status colors
# ─────────────────────────────────────────────────────────────────────────────
STATUS_COLOR = {
    "ok":     "#2ecc71",   # green
    "manual": "#f39c12",   # orange
    "plan":   "#95a5a6",   # gray
}
STATUS_LABEL = {
    "ok":     "✓",
    "manual": "⚠",
    "plan":   "○",
}

# ─────────────────────────────────────────────────────────────────────────────
#  Layout engine
# ─────────────────────────────────────────────────────────────────────────────

NODE_H     = 0.60    # node height (inch)
LEAF_GAP   = 0.22    # gap between leaves (inch)
CLASS_GAP  = 0.55    # gap between classes (inch)
ROOT_X     = 0.0     # x root
CLASS_X    = 2.4     # x class node
LEAF_X     = 4.9     # x leaf node
DESC_X     = 7.0     # x description


def _count_height(node: dict) -> float:
    """Total height of a subtree (in leaf+gap units)."""
    children = node.get("children", [])
    if not children:
        return NODE_H + LEAF_GAP
    return sum(_count_height(c) for c in children) + CLASS_GAP


def _assign_y(node: dict, y_top: float) -> float:
    """Recursively assign 'y_center' to each node. Returns y_bottom."""
    children = node.get("children", [])
    if not children:
        node["y_center"] = y_top - NODE_H / 2
        return y_top - NODE_H - LEAF_GAP

    # class node
    y = y_top
    for child in children:
        y = _assign_y(child, y)

    y_centers = [c["y_center"] for c in children]
    node["y_center"] = (y_centers[0] + y_centers[-1]) / 2
    return y - CLASS_GAP


def _draw_box(ax, x, y, w, h, label, color, text_color="white",
              fontsize=7.5, bold=False, alpha=1.0, radius=0.12):
    box = FancyBboxPatch((x - w/2, y - h/2), w, h,
                         boxstyle=f"round,pad=0.04,rounding_size={radius}",
                         facecolor=color, edgecolor="none",
                         alpha=alpha, zorder=3)
    ax.add_patch(box)
    weight = "bold" if bold else "normal"
    ax.text(x, y, label, ha="center", va="center",
            fontsize=fontsize, color=text_color, weight=weight,
            zorder=4, linespacing=1.3)


def _draw_curve(ax, x0, y0, x1, y1, color, lw=1.3):
    """Bezier S-curve from (x0,y0) to (x1,y1)."""
    from matplotlib.path import Path as MPath
    from matplotlib.patches import PathPatch
    xm = (x0 + x1) / 2
    verts = [(x0, y0), (xm, y0), (xm, y1), (x1, y1)]
    codes = [MPath.MOVETO, MPath.CURVE4, MPath.CURVE4, MPath.CURVE4]
    path  = MPath(verts, codes)
    patch = PathPatch(path, facecolor="none", edgecolor=color,
                      lw=lw, zorder=2, alpha=0.85)
    ax.add_patch(patch)


def generate_mindmap(out_dir: Path):
    # ── Compute layout ─────────────────────────────────────────────────────
    classes   = TREE["children"]
    total_h   = sum(_count_height(c) for c in classes)
    y_top     = total_h / 2

    y = y_top
    for cls in classes:
        y = _assign_y(cls, y)

    TREE["y_center"] = 0.0

    # ── Figure ─────────────────────────────────────────────────────────────
    fig_w = 17
    fig_h = max(total_h + 2, 22)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(-1.5, 12.5)
    ax.set_ylim(-fig_h/2 - 0.5, fig_h/2 + 1.0)
    ax.axis("off")
    fig.patch.set_facecolor("#f8f9fa")
    ax.set_facecolor("#f8f9fa")

    # ── Title ──────────────────────────────────────────────────────────────
    ax.text(5.5, fig_h/2 + 0.7,
            f"SeisWork — Framework Architecture {_SW_VERSION}",
            ha="center", va="center", fontsize=15, weight="bold",
            color="#1a1a2e",
            fontfamily="monospace")
    ax.text(5.5, fig_h/2 + 0.15,
            "Author: HakimBMKG · BMKG · 2026",
            ha="center", va="center", fontsize=10, color="#555")

    # ── Root node ──────────────────────────────────────────────────────────
    _draw_box(ax, ROOT_X, TREE["y_center"], 2.0, 0.75,
              TREE["label"], TREE["color"], "white",
              fontsize=11, bold=True, radius=0.18)

    # ── Legend ─────────────────────────────────────────────────────────────
    legend_x = 11.5
    legend_y = fig_h/2 - 0.5
    for status, col in STATUS_COLOR.items():
        sym = STATUS_LABEL[status]
        desc_map = {"ok": "implemented", "manual": "manual install", "plan": "planned"}
        ax.text(legend_x, legend_y, f"  {sym}  {desc_map[status]}",
                ha="left", va="center", fontsize=9, color=col, weight="bold")
        legend_y -= 0.45

    # ── Class nodes + children ─────────────────────────────────────────────
    for cls in classes:
        cy   = cls["y_center"]
        ccol = cls["color"]

        # line root -> class
        _draw_curve(ax, ROOT_X + 1.0, TREE["y_center"],
                    CLASS_X - 0.95, cy, ccol, lw=1.8)

        # class box
        _draw_box(ax, CLASS_X, cy, 1.9, 0.55,
                  cls["label"], ccol, "white",
                  fontsize=9, bold=True, radius=0.13)

        for leaf in cls.get("children", []):
            ly   = leaf["y_center"]
            st   = leaf.get("status", "ok")
            lcol = STATUS_COLOR.get(st, "#2ecc71")
            sym  = STATUS_LABEL.get(st, "✓")
            desc = leaf.get("desc", "")

            # line class -> leaf
            _draw_curve(ax, CLASS_X + 0.95, cy,
                        LEAF_X - 0.95, ly, ccol, lw=1.2)

            # leaf box
            _draw_box(ax, LEAF_X, ly, 1.9, 0.48,
                      leaf["label"], "#ffffff",
                      text_color="#1a1a2e",
                      fontsize=8.5, alpha=1.0)
            # status color border
            rect = FancyBboxPatch((LEAF_X - 0.95, ly - 0.24), 1.9, 0.48,
                                  boxstyle="round,pad=0.04,rounding_size=0.1",
                                  facecolor="none",
                                  edgecolor=lcol, lw=2.0, zorder=5)
            ax.add_patch(rect)

            # status symbol
            ax.text(LEAF_X + 0.85, ly, sym,
                    ha="center", va="center",
                    fontsize=8, color=lcol, weight="bold", zorder=6)

            # description
            if desc:
                ax.text(DESC_X, ly, desc,
                        ha="left", va="center",
                        fontsize=7.5, color="#555",
                        linespacing=1.35)

    # ── Save ───────────────────────────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    png_path = out_dir / "seiswork_mindmap.png"
    pdf_path = out_dir / "seiswork_mindmap.pdf"

    fig.savefig(str(png_path), dpi=180, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"[Mindmap] PNG → {png_path}")
    try:
        fig.savefig(str(pdf_path), bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        print(f"[Mindmap] PDF → {pdf_path}")
    except Exception as e:
        print(f"[Mindmap] PDF failed (skipping): {e}")

    plt.close(fig)
    return str(png_path)


if __name__ == "__main__":
    base = Path(__file__).parent
    out  = generate_mindmap(base)
    print(f"\nOpen with:\n  eog {out}\n  evince {base/'seiswork_mindmap.pdf'}")

#!/usr/bin/env python3
"""
SeisWork - Pipeline Function Flow Diagram
Author : HakimBMKG

Generates a diagram of the SeisWork pipeline showing how data flows
between modules, from waveform input to the final catalog.

Output: docs/seiswork_flowmap.png
        docs/seiswork_flowmap.pdf

Run:
    conda activate seiswork
    python docs/generate_flowmap.py
"""

from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from matplotlib.path import Path as MPath
from matplotlib.patches import PathPatch
import numpy as np

# ─────────────────────────────────────────────────────────────────────────────
#  Colors per stage (consistent with the mindmap)
# ─────────────────────────────────────────────────────────────────────────────
C = {
    "input"      : "#0f3460",
    "picker"     : "#4361ee",
    "assoc"      : "#7b2d8b",
    "locate"     : "#3a86ff",
    "magnitude"  : "#e63946",
    "velocity"   : "#2dc653",
    "relocation" : "#c77dff",
    "detection"  : "#ff6b35",
    "crosscorr"  : "#e9c46a",
    "infra"      : "#1d3557",
    "file"       : "#f8f9fa",
    "file_border": "#adb5bd",
    "bg"         : "#0d1117",
    "text_dark"  : "#1a1a2e",
    "text_light" : "#ffffff",
    "text_file"  : "#343a40",
    "arrow"      : "#6c757d",
}

# ─────────────────────────────────────────────────────────────────────────────
#  Helper: draw a box with a label + method subtitle
# ─────────────────────────────────────────────────────────────────────────────

def box(ax, cx, cy, w, h, label, methods, color, fontsize_label=9.5,
        fontsize_method=7.5, radius=0.18, alpha=1.0):
    patch = FancyBboxPatch(
        (cx - w / 2, cy - h / 2), w, h,
        boxstyle=f"round,pad=0.06,rounding_size={radius}",
        facecolor=color, edgecolor="none", alpha=alpha, zorder=4,
    )
    ax.add_patch(patch)

    # main label
    ax.text(cx, cy + (0.14 if methods else 0.0), label,
            ha="center", va="center", fontsize=fontsize_label,
            color=C["text_light"], weight="bold", zorder=5)

    # method list
    if methods:
        met_str = "  ·  ".join(methods)
        ax.text(cx, cy - 0.22, met_str,
                ha="center", va="center", fontsize=fontsize_method,
                color="rgba(255,255,255,0.85)" if False else "#ffffffcc",
                alpha=0.90, zorder=5, style="italic")


def file_tag(ax, cx, cy, label, color_border=None):
    """Small CSV/TXT file label between two stages."""
    bdr = color_border or C["file_border"]
    patch = FancyBboxPatch(
        (cx - 0.85, cy - 0.18), 1.70, 0.36,
        boxstyle="round,pad=0.04,rounding_size=0.08",
        facecolor=C["file"], edgecolor=bdr, lw=1.5, zorder=6,
    )
    ax.add_patch(patch)
    ax.text(cx, cy, label, ha="center", va="center",
            fontsize=7, color=C["text_file"], weight="bold", zorder=7)


def arrow_v(ax, x, y0, y1, color, lw=1.8):
    """Vertical downward arrow."""
    ax.annotate("",
                xy=(x, y1 + 0.04), xytext=(x, y0 - 0.04),
                arrowprops=dict(arrowstyle="-|>", color=color,
                                lw=lw, mutation_scale=14),
                zorder=3)


def arrow_curve(ax, x0, y0, x1, y1, color, lw=1.6):
    """Bezier arrow from (x0,y0) to (x1,y1)."""
    xm = (x0 + x1) / 2
    verts = [(x0, y0), (xm, y0), (xm, y1), (x1, y1)]
    codes = [MPath.MOVETO, MPath.CURVE4, MPath.CURVE4, MPath.CURVE4]
    path  = MPath(verts, codes)
    ax.add_patch(PathPatch(path, facecolor="none", edgecolor=color,
                           lw=lw, zorder=3, alpha=0.80))
    # arrowhead
    ax.annotate("",
                xy=(x1, y1), xytext=(x1, y1 + 0.01),
                arrowprops=dict(arrowstyle="-|>", color=color,
                                lw=lw, mutation_scale=13),
                zorder=4)


def hline(ax, x0, x1, y, color, lw=1.5, style="--"):
    ax.plot([x0, x1], [y, y], color=color, lw=lw, ls=style,
            zorder=2, alpha=0.55)


# ─────────────────────────────────────────────────────────────────────────────
#  Main layout coordinates
# ─────────────────────────────────────────────────────────────────────────────
# X columns: 0=infra-left  4=main  8=velocity  8=reloc
# Y rows (up=positive):

MAIN_X   = 4.8    # main pipeline column
VEL_X    = 1.5    # velocity column (bottom left)
RELOC_X  = 8.0    # relocation column (bottom right)
DET_X    = 8.0    # detection column
CC_X     = 8.0    # crosscorr column

BOX_W    = 3.6    # stage box width
BOX_H    = 0.92   # stage box height
FILE_DY  = 0.52   # gap to file-tag above
STEP_DY  = 1.70   # vertical gap between stages

# Y posisi (top-down)
Y_INPUT  = 24.0
Y_PICK   = 21.5
Y_ASSOC  = 18.8
Y_LOC    = 16.1
Y_MAG    = 13.4
Y_VEL    = 10.6
Y_RELOC  = 10.6
Y_DET    = 7.9
Y_CC     = 5.2


def generate_flowmap(out_dir: Path):
    fig_w, fig_h = 16.0, 28.0
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.axis("off")
    fig.patch.set_facecolor(C["bg"])
    ax.set_facecolor(C["bg"])

    # ── Title ──────────────────────────────────────────────────────────────
    ax.text(fig_w / 2, fig_h - 0.5,
            "SeisWork — Alur Fungsi Pipeline",
            ha="center", va="center", fontsize=17, weight="bold",
            color=C["text_light"], fontfamily="monospace")
    ax.text(fig_w / 2, fig_h - 1.0,
            "Author: HakimBMKG · BMKG · 2026",
            ha="center", va="center", fontsize=10, color="#adb5bd")

    # ══════════════════════════════════════════════════════════════════════
    #  INPUT DATA
    # ══════════════════════════════════════════════════════════════════════
    box(ax, MAIN_X, Y_INPUT, BOX_W, BOX_H,
        "INPUT DATA", ["SDS Archive", "FDSN Web Service", "Upload Waveforms"],
        C["input"])

    # arrow INPUT -> PICKER
    arrow_v(ax, MAIN_X, Y_INPUT - BOX_H / 2, Y_PICK + BOX_H / 2, "#adb5bd")

    # ══════════════════════════════════════════════════════════════════════
    #  PICKER
    # ══════════════════════════════════════════════════════════════════════
    box(ax, MAIN_X, Y_PICK, BOX_W, BOX_H,
        "PICKER",
        ["PhaseNet", "PhaseNet Native (Docker/TF)", "EQTransformer", "STALTA"],
        C["picker"])

    file_tag(ax, MAIN_X, (Y_PICK + Y_ASSOC) / 2, "picks.csv", C["picker"])
    arrow_v(ax, MAIN_X, Y_PICK - BOX_H / 2, Y_ASSOC + BOX_H / 2, C["picker"])

    # ══════════════════════════════════════════════════════════════════════
    #  ASSOCIATOR
    # ══════════════════════════════════════════════════════════════════════
    box(ax, MAIN_X, Y_ASSOC, BOX_W, BOX_H,
        "ASSOCIATOR",
        ["GaMMA  (DBSCAN + Bayesian GMM)", "REAL  (grid-search)"],
        C["assoc"])

    file_tag(ax, MAIN_X, (Y_ASSOC + Y_LOC) / 2, "catalog_assoc.csv", C["assoc"])
    arrow_v(ax, MAIN_X, Y_ASSOC - BOX_H / 2, Y_LOC + BOX_H / 2, C["assoc"])

    # ══════════════════════════════════════════════════════════════════════
    #  LOCATOR
    # ══════════════════════════════════════════════════════════════════════
    box(ax, MAIN_X, Y_LOC, BOX_W, BOX_H,
        "LOCATOR",
        ["NLLoc  (probabilistik 3D)", "LocSAT  (SeisComP)", "Hypoinverse"],
        C["locate"])

    file_tag(ax, MAIN_X, (Y_LOC + Y_MAG) / 2, "catalog_loc.csv", C["locate"])
    arrow_v(ax, MAIN_X, Y_LOC - BOX_H / 2, Y_MAG + BOX_H / 2, C["locate"])

    # ══════════════════════════════════════════════════════════════════════
    #  MAGNITUDE
    # ══════════════════════════════════════════════════════════════════════
    box(ax, MAIN_X, Y_MAG, BOX_W, BOX_H,
        "MAGNITUDE",
        ["ML  (Wood-Anderson · Richter 1935)"],
        C["magnitude"])

    file_tag(ax, MAIN_X, (Y_MAG + (Y_VEL + Y_RELOC) / 2) / 2,
             "catalog_ml.csv", C["magnitude"])

    # Fork: arrow left to VELOCITY, right to RELOCATION
    fork_y  = Y_MAG - BOX_H / 2 - 0.35   # fork point
    fork_y2 = fork_y - 0.55               # after file-tag

    arrow_v(ax, MAIN_X, Y_MAG - BOX_H / 2, fork_y, C["magnitude"])
    # horizontal line to the left (velocity)
    ax.plot([VEL_X, MAIN_X], [fork_y2, fork_y2],
            color=C["magnitude"], lw=1.8, alpha=0.7, zorder=3)
    # horizontal line to the right (relocation)
    ax.plot([MAIN_X, RELOC_X], [fork_y2, fork_y2],
            color=C["magnitude"], lw=1.8, alpha=0.7, zorder=3)
    # arrow down to VELOCITY
    arrow_v(ax, VEL_X, fork_y2, Y_VEL + BOX_H / 2, C["velocity"])
    # arrow down to RELOCATION
    arrow_v(ax, RELOC_X, fork_y2, Y_RELOC + BOX_H / 2, C["relocation"])

    # ══════════════════════════════════════════════════════════════════════
    #  VELOCITY (left)
    # ══════════════════════════════════════════════════════════════════════
    box(ax, VEL_X, Y_VEL, 2.80, BOX_H,
        "VELOCITY",
        ["VELEST  (1D P Kissling 1994)"],
        C["velocity"])

    # output velocity
    out_vel_y = Y_VEL - BOX_H / 2 - 0.26
    file_tag(ax, VEL_X, out_vel_y, "velmod_jailolo.txt", C["velocity"])
    arrow_v(ax, VEL_X, Y_VEL - BOX_H / 2, out_vel_y + 0.18 - 0.04, C["velocity"])

    # ══════════════════════════════════════════════════════════════════════
    #  RELOCATION (right)
    # ══════════════════════════════════════════════════════════════════════
    box(ax, RELOC_X, Y_RELOC, 2.80, BOX_H,
        "RELOCATION",
        ["HypoDD  (double-difference)", "GrowClust  (waveform CC)"],
        C["relocation"])

    file_tag(ax, RELOC_X, (Y_RELOC + Y_DET) / 2,
             "catalog_reloc.csv", C["relocation"])
    arrow_v(ax, RELOC_X, Y_RELOC - BOX_H / 2, Y_DET + BOX_H / 2, C["relocation"])

    # ══════════════════════════════════════════════════════════════════════
    #  DETECTION (right)
    # ══════════════════════════════════════════════════════════════════════
    box(ax, DET_X, Y_DET, 2.80, BOX_H,
        "DETECTION",
        ["Match&Locate  (template matching)"],
        C["detection"])

    file_tag(ax, DET_X, (Y_DET + Y_CC) / 2,
             "catalog_detect.csv", C["detection"])
    arrow_v(ax, DET_X, Y_DET - BOX_H / 2, Y_CC + BOX_H / 2, C["detection"])

    # ══════════════════════════════════════════════════════════════════════
    #  CROSSCORR ANALYSIS (right)
    # ══════════════════════════════════════════════════════════════════════
    box(ax, CC_X, Y_CC, 2.80, BOX_H,
        "CROSSCORR",
        ["FDTCC  (dt.cc + dt.ct)", "Statistik · Waveform · RMS modal"],
        C["crosscorr"], fontsize_label=9.5, fontsize_method=7.0)

    # output crosscorr
    out_cc_y = Y_CC - BOX_H / 2 - 0.26
    file_tag(ax, CC_X, out_cc_y, "dt.cc / dt.ct / growclust_in", C["crosscorr"])
    arrow_v(ax, CC_X, Y_CC - BOX_H / 2, out_cc_y + 0.18 - 0.04, C["crosscorr"])

    # ══════════════════════════════════════════════════════════════════════
    #  IO / CONVERTER (small panel right of the main pipeline)
    # ══════════════════════════════════════════════════════════════════════
    io_x  = 12.9
    io_y  = (Y_ASSOC + Y_LOC) / 2 + 0.3
    io_w  = 2.80
    io_h  = 3.20
    io_items = [
        "picks → VELEST (.pha)",
        "picks → NLLoc (NLLOC_OBS)",
        "picks → HypoDD (phase.dat)",
        "picks → Hypoinverse (ARC)",
        "picks → SCML (SeisComP XML)",
    ]
    patch_io = FancyBboxPatch(
        (io_x - io_w / 2, io_y - io_h / 2), io_w, io_h,
        boxstyle="round,pad=0.08,rounding_size=0.18",
        facecolor=C["infra"], edgecolor="#3d5a80", lw=1.5,
        alpha=0.92, zorder=4,
    )
    ax.add_patch(patch_io)
    ax.text(io_x, io_y + io_h / 2 - 0.30, "IO / Converter",
            ha="center", va="center", fontsize=9, weight="bold",
            color=C["text_light"], zorder=5)
    for i, itm in enumerate(io_items):
        ax.text(io_x - io_w / 2 + 0.18,
                io_y + io_h / 2 - 0.65 - i * 0.48,
                f"• {itm}",
                ha="left", va="center", fontsize=7.2,
                color="#adb5bd", zorder=5)

    # ══════════════════════════════════════════════════════════════════════
    #  INFRASTRUCTURE (bottom panel)
    # ══════════════════════════════════════════════════════════════════════
    infra_y  = 2.2
    infra_h  = 2.60
    infra_w  = fig_w - 1.2
    infra_x  = fig_w / 2

    patch_inf = FancyBboxPatch(
        (infra_x - infra_w / 2, infra_y - infra_h / 2),
        infra_w, infra_h,
        boxstyle="round,pad=0.10,rounding_size=0.22",
        facecolor=C["infra"], edgecolor="#3d5a80", lw=1.8,
        alpha=0.93, zorder=4,
    )
    ax.add_patch(patch_inf)
    ax.text(infra_x, infra_y + infra_h / 2 - 0.28,
            "INFRASTRUKTUR & ORKESTRASI",
            ha="center", va="center", fontsize=10, weight="bold",
            color=C["text_light"], zorder=5)

    infra_items = [
        ("parallel.py",    "Orchestrator chunked ProcessPool · 37 mnt/106 event"),
        ("Web GUI",        "Flask port 5000 · config · monitoring · viewer"),
        ("Federation",     "Server↔Client REST API · bearer-token · /api/health"),
        ("Config (YAML)",  "region · station · velocity model · NLLoc grid"),
    ]
    cols = 2
    dx   = infra_w / cols
    for i, (nm, desc) in enumerate(infra_items):
        col = i % cols
        row = i // cols
        tx  = infra_x - infra_w / 2 + dx * col + dx / 2
        ty  = infra_y + 0.14 - row * 0.72
        ax.text(tx, ty, nm, ha="center", va="center",
                fontsize=8.5, weight="bold", color="#a8dadc", zorder=5)
        ax.text(tx, ty - 0.30, desc, ha="center", va="center",
                fontsize=7.0, color="#adb5bd", zorder=5)

    # ══════════════════════════════════════════════════════════════════════
    #  LEGEND (top-right corner)
    # ══════════════════════════════════════════════════════════════════════
    leg_x  = 13.2
    leg_y  = fig_h - 1.9
    leg_w  = 2.50
    leg_h  = 5.60
    patch_leg = FancyBboxPatch(
        (leg_x - 0.15, leg_y - leg_h + 0.15), leg_w, leg_h,
        boxstyle="round,pad=0.08,rounding_size=0.18",
        facecolor="#161b22", edgecolor="#30363d", lw=1.2,
        alpha=0.96, zorder=8,
    )
    ax.add_patch(patch_leg)
    ax.text(leg_x + leg_w / 2 - 0.15, leg_y - 0.20,
            "Legenda", ha="center", fontsize=9, weight="bold",
            color=C["text_light"], zorder=9)

    leg_items = [
        (C["picker"],     "Picker"),
        (C["assoc"],      "Associator"),
        (C["locate"],     "Locator"),
        (C["magnitude"],  "Magnitude"),
        (C["velocity"],   "Velocity"),
        (C["relocation"], "Relocation"),
        (C["detection"],  "Detection"),
        (C["crosscorr"],  "CrossCorr"),
        (C["infra"],      "Infrastruktur"),
    ]
    for j, (col, lbl) in enumerate(leg_items):
        ly = leg_y - 0.52 - j * 0.52
        patch_sm = FancyBboxPatch(
            (leg_x, ly - 0.15), 0.36, 0.30,
            boxstyle="round,pad=0.03,rounding_size=0.06",
            facecolor=col, edgecolor="none", zorder=9,
        )
        ax.add_patch(patch_sm)
        ax.text(leg_x + 0.50, ly, lbl, ha="left", va="center",
                fontsize=8, color=C["text_light"], zorder=9)

    # ══════════════════════════════════════════════════════════════════════
    #  Label sub-panel (parallel.py note)
    # ══════════════════════════════════════════════════════════════════════
    ax.text(0.55, Y_PICK, "parallel.py\n(chunked\norchestrator)",
            ha="center", va="center", fontsize=6.8,
            color="#adb5bd", style="italic",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#161b22",
                      edgecolor="#30363d", lw=1.0, alpha=0.85))

    # ── Save ──────────────────────────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    png_path = out_dir / "seiswork_flowmap.png"
    pdf_path = out_dir / "seiswork_flowmap.pdf"

    fig.savefig(str(png_path), dpi=180, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"[Flowmap] PNG → {png_path}")
    try:
        fig.savefig(str(pdf_path), bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        print(f"[Flowmap] PDF → {pdf_path}")
    except Exception as e:
        print(f"[Flowmap] PDF failed: {e}")

    plt.close(fig)
    return str(png_path)


if __name__ == "__main__":
    base = Path(__file__).parent
    out  = generate_flowmap(base)
    print(f"\nOpen with:\n  eog {out}\n  evince {base / 'seiswork_flowmap.pdf'}")

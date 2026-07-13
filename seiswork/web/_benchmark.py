#!/usr/bin/env python3
"""
SeisWork - Benchmark helpers
Author : HakimBMKG

Parses catalog/location outputs (REAL, VELEST, hypoDD, GrowClust, magnitude
phase files) into a common per-event frame and summarizes RMS/depth/magnitude/
gap, so the GUI Benchmark panel can compare SeisWork results against reference
catalogs.

Each parser returns a DataFrame with columns:
  event_id, lat, lon, depth_km, mag, rms, gap
"""

import os
import glob

import numpy as np
import pandas as pd

COLS = ["event_id", "lat", "lon", "depth_km", "mag", "rms", "gap"]


def _empty():
    return pd.DataFrame(columns=COLS)


# ── VELEST final.CNV ──────────────────────────────────────────────────────────
def parse_velest_cnv(path: str) -> pd.DataFrame:
    """CNV header line: YY MMDD HHMM SEC  LATn LONe  DEP  MAG  GAP  RMS
    followed by phase lines (6-char station blocks). Header lines carry the
    N/S, E/W hemisphere letters."""
    if not path or not os.path.exists(path):
        return _empty()
    rows = []
    with open(path) as f:
        for ln in f:
            s = ln.rstrip("\n")
            # header line has a latitude like '1.1669N' and longitude '127.4609E'
            if "N" not in s and "S" not in s:
                continue
            try:
                lat_tok = next(t for t in s.split() if t[-1] in "NS" and any(c.isdigit() for c in t))
                lon_tok = next(t for t in s.split() if t[-1] in "EW" and any(c.isdigit() for c in t))
            except StopIteration:
                continue
            p = s.split()
            try:
                lat = float(lat_tok[:-1]) * (-1 if lat_tok[-1] == "S" else 1)
                lon = float(lon_tok[:-1]) * (-1 if lon_tok[-1] == "W" else 1)
                # tokens after lon: dep mag gap rms
                li = p.index(lon_tok)
                dep = float(p[li + 1]); mag = float(p[li + 2])
                gap = float(p[li + 3]) if len(p) > li + 3 else np.nan
                rms = float(p[li + 4]) if len(p) > li + 4 else np.nan
                rows.append([len(rows) + 1, lat, lon, dep, mag, rms, gap])
            except (ValueError, IndexError):
                continue
    return pd.DataFrame(rows, columns=COLS)


# ── hypoDD.reloc ──────────────────────────────────────────────────────────────
def parse_hypodd_reloc(path: str) -> pd.DataFrame:
    """hypoDD.reloc 24-col: 1 ID 2 LAT 3 LON 4 DEP ... 17 MAG 18 NCCP 19 NCCS
    20 NCTP 21 NCTS 22 RCC 23 RCT 24 CID. We report RCT (catalog dd residual,
    ms) as RMS, falling back to RCC when RCT is unset (-9)."""
    if not path or not os.path.exists(path):
        return _empty()
    rows = []
    for ln in open(path):
        p = ln.split()
        if len(p) < 17:
            continue
        try:
            mag = float(p[16]) if len(p) > 16 else np.nan
            rct = float(p[22]) if len(p) > 22 else np.nan
            rcc = float(p[21]) if len(p) > 21 else np.nan
            rms = rct if (not np.isnan(rct) and rct > -9) else rcc
            if rms is not None and rms <= -9:
                rms = np.nan
            rows.append([p[0], float(p[1]), float(p[2]), float(p[3]),
                         mag, rms, np.nan])
        except (ValueError, IndexError):
            continue
    return pd.DataFrame(rows, columns=COLS)


# ── GrowClust out.growclust_cat ───────────────────────────────────────────────
def parse_growclust_cat(path: str) -> pd.DataFrame:
    """GrowClust cat: ... 6 evid 7 latR 8 lonR 9 depR 10 mag ... 17 rmsP
    18 rmsS ... We report mean(rmsP, rmsS) as RMS."""
    if not path or not os.path.exists(path):
        return _empty()
    rows = []
    for ln in open(path):
        p = ln.split()
        if len(p) < 11:
            continue
        try:
            rmsP = float(p[17]) if len(p) > 17 else np.nan
            rmsS = float(p[18]) if len(p) > 18 else np.nan
            vals = [v for v in (rmsP, rmsS) if not np.isnan(v) and v >= 0]
            rms = float(np.mean(vals)) if vals else np.nan
            rows.append([p[6], float(p[7]), float(p[8]), float(p[9]),
                         float(p[10]), rms, np.nan])
        except (ValueError, IndexError):
            continue
    return pd.DataFrame(rows, columns=COLS)


# ── REAL catalog_sel.txt (one file or a directory of *.catalog_sel.txt) ───────
def parse_real_catalog(path: str) -> pd.DataFrame:
    """REAL catalog_sel columns: num yr mo dy time secOfDay residual lat lon
    dep mag magres ... gap(last). residual(col7, idx6) is the traveltime RMS."""
    files = []
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.catalog_sel.txt")))
    elif os.path.exists(path):
        files = [path]
    rows = []
    for fp in files:
        for ln in open(fp):
            p = ln.split()
            if len(p) < 11:
                continue
            try:
                # REAL does not output a calibrated magnitude in catalog_sel
                # (the column after depth is not ML); leave mag unset.
                rows.append([f"{os.path.basename(fp)[:8]}_{p[0]}",
                             float(p[7]), float(p[8]), float(p[9]),
                             np.nan, float(p[6]), float(p[-1])])
            except (ValueError, IndexError):
                continue
    return pd.DataFrame(rows, columns=COLS)


# ── magnitude phase catalog (hypoDD-phase: '# yr mo dy hr mn sc lat lon dep mag') ─
def parse_mag_phase(path: str) -> pd.DataFrame:
    if not path or not os.path.exists(path):
        return _empty()
    rows = []
    for ln in open(path):
        if not ln.startswith("#"):
            continue
        p = ln.split()
        if len(p) < 11:
            continue
        try:
            rows.append([p[-1], float(p[7]), float(p[8]), float(p[9]),
                         float(p[10]), np.nan, np.nan])
        except (ValueError, IndexError):
            continue
    return pd.DataFrame(rows, columns=COLS)


# ── generic SeisWork catalog CSV ──────────────────────────────────────────────
def parse_csv_catalog(path: str) -> pd.DataFrame:
    if not path or not os.path.exists(path):
        return _empty()
    try:
        df = pd.read_csv(path)
    except Exception:
        return _empty()
    out = pd.DataFrame()
    out["event_id"] = df.get("event_id", pd.Series(range(len(df))))
    for c, src in [("lat", "lat"), ("lon", "lon"), ("depth_km", "depth_km"),
                   ("mag", "mag"), ("rms", "rms"), ("gap", "gap")]:
        out[c] = pd.to_numeric(df.get(src), errors="coerce") if src in df else np.nan
    return out[COLS]


# ── summary statistics for one catalog ────────────────────────────────────────
def summarize(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {"n": 0}
    def med(c):
        v = pd.to_numeric(df[c], errors="coerce").dropna()
        return None if v.empty else round(float(v.median()), 4)
    def mean(c):
        v = pd.to_numeric(df[c], errors="coerce").dropna()
        return None if v.empty else round(float(v.mean()), 4)
    rms = pd.to_numeric(df["rms"], errors="coerce").dropna()
    return {
        "n"          : int(len(df)),
        "rms_median" : med("rms"),
        "rms_mean"   : mean("rms"),
        "rms_n"      : int(len(rms)),
        "depth_median": med("depth_km"),
        "mag_median" : med("mag"),
        "gap_median" : med("gap"),
        # coarse RMS histogram (for overlay), 0..1.0 s in 20 bins
        "rms_hist"   : _rms_hist(rms),
    }


def _rms_hist(rms: pd.Series, hi: float = 1.0, nb: int = 20):
    if rms is None or len(rms) == 0:
        return {"edges": [], "counts": []}
    counts, edges = np.histogram(np.clip(rms, 0, hi), bins=nb, range=(0, hi))
    return {"edges": [round(float(e), 3) for e in edges],
            "counts": [int(c) for c in counts]}

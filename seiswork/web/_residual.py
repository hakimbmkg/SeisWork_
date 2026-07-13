"""Residual diagnostics for HypoDD relocation (by HakimBMKG).

Reproduces the analysis of `residual_hypodd.ipynb` (folder hypodd_filter903)
as a JSON report consumable by the SeisWork GUI "Residual HypoDD" tab.

It reads, from a finished relocation job directory:
  - hypoDD.res   : event-pair residuals
  - hypoDD.log   : per-iteration RMS convergence (best effort)
  - ph2dt.inp    : ph2dt parameters used
  - hypoDD.inp   : hypoDD parameters used

Two hypoDD.res layouts are supported transparently:

  A) HYPODD v2.1beta (SeisWork core, has a header line starting with "STA"):
       STA  DT  C1  C2  IDX  QUAL  RES[ms]  WT  OFFS
     -> residual  = RES[ms] / 1000   (seconds)
       phase     = IDX (3=P, 4=S)
       dist_km   = OFFS / 1000      (OFFS in metres)

  B) Legacy / simulflow (no header, 9 numeric columns):
       STA  DT  EV1  EV2  PHA  WT  AZ  CC  DIST
     -> residual  = DT                (already seconds)
       phase     = PHA (3=P, 4=S)
       dist_km   = DIST / 1000
"""
from __future__ import annotations

import math
import os
import re
from typing import Optional

import numpy as np


# ── low-level parsing ────────────────────────────────────────────────────────
def parse_hypodd_res(path: str) -> dict:
    """Return dict with numpy arrays: sta, res_s, phase('P'/'S'), dist_km, ev1, ev2.

    Raises FileNotFoundError if the file is missing, ValueError if empty.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    with open(path, errors="replace") as fh:
        lines = [ln for ln in fh if ln.strip()]
    if not lines:
        raise ValueError("hypoDD.res is empty")

    first = lines[0].strip()
    has_header = first.upper().startswith("STA") and not _is_number(first.split()[1])

    sta, res_s, phase, dist_km, ev1, ev2 = [], [], [], [], [], []
    data_lines = lines[1:] if has_header else lines

    for ln in data_lines:
        p = ln.split()
        if len(p) < 9:
            continue
        try:
            if has_header:
                # STA DT C1 C2 IDX QUAL RES[ms] WT OFFS
                s   = p[0]
                e1  = int(p[2]); e2 = int(p[3])
                idx = int(p[4])
                r   = float(p[6]) / 1000.0
                d   = float(p[8]) / 1000.0
            else:
                # STA DT C1 C2 IDX QUAL RES[ms] WT OFFS  (same layout, no header line)
                s   = p[0]
                e1  = int(p[2]); e2 = int(p[3])
                idx = int(p[4])
                r   = float(p[6]) / 1000.0
                d   = float(p[8]) / 1000.0
        except (ValueError, IndexError):
            continue
        ph = "P" if idx == 3 else ("S" if idx == 4 else "?")
        sta.append(s); res_s.append(r); phase.append(ph)
        dist_km.append(d); ev1.append(e1); ev2.append(e2)

    return {
        "sta"    : np.array(sta),
        "res_s"  : np.array(res_s, dtype=float),
        "phase"  : np.array(phase),
        "dist_km": np.array(dist_km, dtype=float),
        "ev1"    : np.array(ev1, dtype=int),
        "ev2"    : np.array(ev2, dtype=int),
        "format" : "v2.1beta" if has_header else "legacy",
    }


def _is_number(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False


def parse_hypodd_log(path: str) -> list:
    """Per-iteration convergence rows from hypoDD.log (best effort).

    The convergence table is bounded by its header (a line containing both
    "IT" and "RMSCT") and the "writing out results" marker. Only lines inside
    that region are parsed, so velocity-model rows and other numeric dumps are
    never mistaken for iterations. Each iteration line looks like
        " 1  1 100 100   91 -13.1   269 ..."  -> IT EV %EV %CT RMSCT dRMS RMSST
    Returns list of {it, ev, pct_ct, rmsct_ms, drms_pct, rmsst_ms}.
    """
    rows = []
    if not os.path.exists(path):
        return rows
    pat = re.compile(
        r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+\.?\d*)\s+([\d.\-]+)\s+(\d+\.?\d*)"
    )
    in_table = False
    with open(path, errors="replace") as fh:
        for line in fh:
            up = line.upper()
            if not in_table:
                # Table header carries both IT and RMSCT column labels.
                if "RMSCT" in up and "RMSST" in up:
                    in_table = True
                continue
            if "WRITING OUT" in up:
                break
            m = pat.match(line)
            if not m:
                # Skip the units sub-header (" % % ms ...") but end the table on
                # a substantive non-matching line (blank lines tolerated).
                if line.strip() and not line.strip().startswith("%"):
                    if any(c.isalpha() for c in line):
                        in_table = False
                continue
            it, ev, _pev, pct, rmsct, drms, rmsst = m.groups()
            if int(it) > 200:           # iteration counter is small; guards noise
                continue
            rows.append({
                "it"      : int(it),
                "ev"      : int(ev),
                "pct_ct"  : float(pct),
                "rmsct_ms": float(rmsct),
                "drms_pct": float(drms),
                "rmsst_ms": float(rmsst),
            })
    return rows


# ── statistics ───────────────────────────────────────────────────────────────
def _skew(d: np.ndarray) -> float:
    n = len(d)
    if n < 3:
        return float("nan")
    m = d.mean(); s = d.std()
    if s == 0:
        return 0.0
    return float(np.mean(((d - m) / s) ** 3))


def _kurt_excess(d: np.ndarray) -> float:
    n = len(d)
    if n < 4:
        return float("nan")
    m = d.mean(); s = d.std()
    if s == 0:
        return 0.0
    return float(np.mean(((d - m) / s) ** 4) - 3.0)


def _stats_row(label: str, d: np.ndarray) -> dict:
    d = d[np.isfinite(d)]
    if d.size == 0:
        return {"fase": label, "n": 0}
    return {
        "fase"        : label,
        "n"           : int(d.size),
        "mean"        : round(float(d.mean()), 4),
        "median"      : round(float(np.median(d)), 4),
        "std"         : round(float(d.std()), 4),
        "rms"         : round(float(np.sqrt(np.mean(d ** 2))), 4),
        "skew"        : round(_skew(d), 3),
        "kurt"        : round(_kurt_excess(d), 3),
        "pct_outlier" : round(float(np.mean(np.abs(d) > 0.3) * 100.0), 2),
        "min"         : round(float(d.min()), 4),
        "max"         : round(float(d.max()), 4),
    }


def _hist(d: np.ndarray, nbins: int = 40) -> dict:
    d = d[np.isfinite(d)]
    if d.size < 2:
        return {"edges": [], "counts": []}
    q_lo, q_hi = np.percentile(d, [1, 99])
    lim = max(abs(q_lo), abs(q_hi)) * 1.15
    if lim <= 0:
        lim = max(abs(d).max(), 0.05)
    edges = np.linspace(-lim, lim, nbins + 1)
    counts, _ = np.histogram(d, bins=edges)
    return {"edges": [round(float(e), 5) for e in edges],
            "counts": [int(c) for c in counts]}


def _per_station(res: dict) -> list:
    """Box-stats (median, q1, q3, whiskers, n) per station for P and S."""
    out = []
    for sta in sorted(set(res["sta"].tolist())):
        msk = res["sta"] == sta
        row = {"sta": sta}
        for ph, key in (("P", "p"), ("S", "s")):
            d = res["res_s"][msk & (res["phase"] == ph)]
            d = d[np.isfinite(d)]
            if d.size == 0:
                row[key] = None
                continue
            q1, med, q3 = np.percentile(d, [25, 50, 75])
            iqr = q3 - q1
            lo = float(d[d >= q1 - 1.5 * iqr].min()) if d.size else float(d.min())
            hi = float(d[d <= q3 + 1.5 * iqr].max()) if d.size else float(d.max())
            row[key] = {
                "n": int(d.size),
                "median": round(float(med), 4),
                "q1": round(float(q1), 4), "q3": round(float(q3), 4),
                "lo": round(lo, 4), "hi": round(hi, 4),
                "mean": round(float(d.mean()), 4),
            }
        out.append(row)
    return out


def _scatter(res: dict, ph: str, max_pts: int = 4000) -> dict:
    msk = res["phase"] == ph
    x = res["dist_km"][msk]; y = res["res_s"][msk]
    good = np.isfinite(x) & np.isfinite(y)
    x, y = x[good], y[good]
    if x.size > max_pts:
        idx = np.random.default_rng(0).choice(x.size, max_pts, replace=False)
        x, y = x[idx], y[idx]
    return {"x": [round(float(v), 3) for v in x],
            "y": [round(float(v), 4) for v in y]}


# ── parameter files ──────────────────────────────────────────────────────────
def _read_text(path: str) -> str:
    try:
        with open(path, errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _parse_ph2dt_inp(path: str) -> Optional[dict]:
    txt = _read_text(path)
    if not txt:
        return None
    nums = []
    for ln in txt.splitlines():
        s = ln.strip()
        if not s or s.startswith("*"):
            continue
        toks = s.split()
        if all(_is_number(t) for t in toks) and len(toks) >= 7:
            nums = toks
    if len(nums) < 7:
        return {"raw": txt}
    keys = ["MINWGHT", "MAXDIST", "MAXSEP", "MAXNGH", "MINLNK", "MINOBS", "MAXOBS"]
    return {"params": dict(zip(keys, nums)), "raw": txt}


def _parse_hypodd_inp(path: str) -> Optional[dict]:
    """Parse hypoDD.inp (newest getinp.f format) into structured parameters.

    getinp.f counts every NON-comment line positionally: comment lines start
    with '*' in column 1/2 and are skipped, but BLANK lines are NOT skipped and
    still consume a slot (e.g. an empty fn_cc when cross-correlation is off).
    The required line sequence is:

        1..9   fn_cc fn_ct fn_eve fn_sta fn_loc fn_reloc fn_stares fn_res fn_srcpar
        10     IDAT IPHA DIST
        11     OBSCC OBSCT
        12     ISTART ISOLV NSET
        13..12+NSET   AITER WTCCP WTCCS WRCC WDCC WTCTP WTCTS WRCT WDCT DAMP
        13+NSET       NLAY RATIO
        14+NSET       TOP(1..NLAY)
        15+NSET       VEL(1..NLAY)
        16+NSET       CID

    Returns {"params": {...}, "raw": txt}; on any structural mismatch it
    degrades gracefully to {"raw": txt} so the GUI can still show the file.
    """
    txt = _read_text(path)
    if not txt:
        return None

    # Drop comment lines ('*' in col 1/2) but keep blank lines: they are real
    # positional slots (fn_cc is commonly blank when CC is disabled).
    rows = []
    for ln in txt.splitlines():
        st = ln.strip()
        if st.startswith("*"):
            continue
        rows.append(st)
    # Trim a trailing run of blanks (e.g. empty CID/ID section at EOF) but keep
    # interior blanks that carry meaning.
    while rows and rows[-1] == "":
        rows.pop()

    try:
        files = {
            "fn_cc"    : rows[0], "fn_ct"     : rows[1], "fn_eve": rows[2],
            "fn_sta"   : rows[3], "fn_loc"    : rows[4], "fn_reloc": rows[5],
            "fn_stares": rows[6], "fn_res"    : rows[7], "fn_srcpar": rows[8],
        }
        idat, ipha, dist = rows[9].split()[:3]
        obs = rows[10].split()
        obscc, obsct = (obs + ["", ""])[:2]
        istart, isolv, nset = rows[11].split()[:3]
        nset_i = int(nset)

        wcols = ["AITER", "WTCCP", "WTCCS", "WRCC", "WDCC",
                 "WTCTP", "WTCTS", "WRCT", "WDCT", "DAMP"]
        weighting = []
        for k in range(nset_i):
            toks = rows[12 + k].split()
            weighting.append(dict(zip(wcols, toks)))

        base = 12 + nset_i
        nlay, ratio = (rows[base].split() + [""])[:2]
        nlay_i = int(nlay)
        tops = rows[base + 1].split()
        vels = rows[base + 2].split()
        cid  = rows[base + 3].split()[0] if len(rows) > base + 3 and rows[base + 3] else "0"

        idat_lbl = {"0": "synthetics", "1": "cross-corr", "2": "catalog",
                    "3": "cross & cat"}.get(str(idat), idat)
        ipha_lbl = {"1": "P", "2": "S", "3": "P&S"}.get(str(ipha), ipha)

        params = {
            "IDAT"  : f"{idat} ({idat_lbl})",
            "IPHA"  : f"{ipha} ({ipha_lbl})",
            "DIST"  : dist,
            "OBSCC" : obscc,
            "OBSCT" : obsct,
            "ISTART": istart,
            "ISOLV" : f"{isolv} ({'SVD' if str(isolv)=='1' else 'LSQR'})",
            "NSET"  : nset,
            "NLAY"  : nlay,
            "RATIO" : ratio,
        }
        return {
            "params"   : params,
            "weighting": {"cols": wcols, "rows": weighting},
            "model"    : {"top": tops, "vel": vels, "ratio": ratio},
            "cid"      : cid,
            "files"    : files,
            "raw"      : txt,
        }
    except (IndexError, ValueError):
        return {"raw": txt}


# ── automatic diagnosis (port of notebook cell 5 / 14) ───────────────────────
def _diagnose(rows: dict, n_pairs: int, n_ev: int) -> list:
    issues = []
    allr = rows.get("All") or rows.get("Semua", {})
    pr   = rows.get("P", {})
    sr   = rows.get("S", {})
    n_all = allr.get("n", 0)
    if n_all == 0:
        return [{"level": "CRITICAL", "msg": "No residual observations (hypoDD.res empty / relocation failed)."}]

    rms_p = pr.get("rms", 0) or 0
    rms_s = sr.get("rms", 0) or 0
    mean_all = allr.get("mean", 0) or 0
    pct_large = allr.get("pct_outlier", 0) or 0

    if n_all < 100:
        issues.append({"level": "CRITICAL",
            "msg": f"Very sparse data: {n_all} obs, {n_pairs} pairs, {n_ev} events. "
                   "Likely ph2dt constraints too tight (MAXNGH/MINLNK/MAXDIST) or catalog too sparse. "
                   "Distribution not representative; outliers dominate the histogram."})
    if rms_p and rms_s > rms_p * 1.4:
        issues.append({"level": "WARNING",
            "msg": f"RMS S ({rms_s:.4f}s) >> RMS P ({rms_p:.4f}s) — ratio {rms_s/rms_p:.2f}x. "
                   "S picks from PhaseNet lack precision; consider lowering WTCTS."})
    if pct_large > 10:
        issues.append({"level": "WARNING",
            "msg": f"{pct_large:.1f}% residuals > 0.3 s. Initial WRCT/WDCT too loose → outliers leak into inversion."})
    if abs(mean_all) >= 0.01:
        direction = "late (Vp model may be too low)" if mean_all > 0 \
               else "early (Vp model may be too high)"
        issues.append({"level": "INFO",
            "msg": f"Mean residual {mean_all:+.4f}s ≠ 0 → origin time systematically {direction}. "
                   "Check velocity model / VELEST."})
    # skew advice
    for lbl, r in (("P", pr), ("S", sr)):
        sk = r.get("skew")
        if sk is not None and not (isinstance(sk, float) and math.isnan(sk)) and abs(sk) >= 0.5:
            direction = "heavy right tail (residuals tend late)" if sk > 0 \
                   else "heavy left tail (residuals tend early)"
            issues.append({"level": "INFO",
                "msg": f"Skew {lbl} = {sk:+.3f}: {direction}. Suggestion: tighten WRCT / check shallow Vp."})
    if not issues:
        issues.append({"level": "OK", "msg": "No serious residual issues detected."})
    return issues


def _criteria(rows: dict) -> list:
    allr = rows.get("All") or rows.get("Semua", {}); pr = rows.get("P", {}); sr = rows.get("S", {})
    mean_all = allr.get("mean", 0) or 0
    out = [
        ("|Mean| < 0.01 s",      abs(mean_all) < 0.01,                 f"Mean = {mean_all:+.4f} s"),
        ("|Skew| P < 0.5",       abs(pr.get("skew", 9) or 9) < 0.5,    f"Skew P = {pr.get('skew')}"),
        ("|Skew| S < 0.5",       abs(sr.get("skew", 9) or 9) < 0.5,    f"Skew S = {sr.get('skew')}"),
        ("RMS P < 0.08 s",       (pr.get("rms", 9) or 9) < 0.08,       f"RMS P = {pr.get('rms')} s"),
        ("RMS S < 0.12 s",       (sr.get("rms", 9) or 9) < 0.12,       f"RMS S = {sr.get('rms')} s"),
        ("% |res|>0.3s < 5%",    (allr.get("pct_outlier", 99) or 99) < 5.0,
                                 f"{allr.get('pct_outlier')}%"),
    ]
    return [{"name": n, "pass": bool(ok), "value": v} for n, ok, v in out]


# ── public: assemble full report ─────────────────────────────────────────────
def compute_residual_report(job_dir: str) -> dict:
    """Build the full residual diagnostics report for one relocation job dir."""
    res_path = os.path.join(job_dir, "hypoDD.res")
    res = parse_hypodd_res(res_path)

    all_r = res["res_s"]
    p_r   = res["res_s"][res["phase"] == "P"]
    s_r   = res["res_s"][res["phase"] == "S"]

    stats = {
        "All": _stats_row("Semua", all_r),
        "P"  : _stats_row("P (cat=3)", p_r),
        "S"  : _stats_row("S (cat=4)", s_r),
    }

    pairs = set(zip(res["ev1"].tolist(), res["ev2"].tolist()))
    evs   = set(res["ev1"].tolist()) | set(res["ev2"].tolist())
    n_pairs = len(pairs)
    n_ev    = len(evs)

    report = {
        "ok"        : True,
        "format"    : res["format"],
        "summary"   : {
            "n_obs"   : int(all_r.size),
            "n_p"     : int(p_r.size),
            "n_s"     : int(s_r.size),
            "n_pairs" : n_pairs,
            "n_events": n_ev,
            "n_sta"   : int(len(set(res["sta"].tolist()))),
            "stations": sorted(set(res["sta"].tolist())),
            "dist_min": round(float(res["dist_km"].min()), 3) if res["dist_km"].size else None,
            "dist_max": round(float(res["dist_km"].max()), 3) if res["dist_km"].size else None,
        },
        "stats"     : [stats["All"], stats["P"], stats["S"]],
        "hist"      : {
            "all": _hist(all_r),
            "p"  : _hist(p_r),
            "s"  : _hist(s_r),
        },
        "scatter"   : {"p": _scatter(res, "P"), "s": _scatter(res, "S")},
        "per_station": _per_station(res),
        "convergence": parse_hypodd_log(os.path.join(job_dir, "hypoDD.log")),
        "criteria"  : _criteria(stats),
        "diagnosis" : _diagnose(stats, n_pairs, n_ev),
        "ph2dt_inp" : _parse_ph2dt_inp(os.path.join(job_dir, "ph2dt.inp")),
        "hypodd_inp": _parse_hypodd_inp(os.path.join(job_dir, "hypoDD.inp")),
    }
    return report

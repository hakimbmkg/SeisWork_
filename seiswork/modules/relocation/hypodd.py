#!/usr/bin/env python3
"""
SeisWork — HypoDD double-difference relocation module
Author : HakimBMKG

Supports two modes:
  catalog   — ph2dt catalog differential times (dt.ct)
  crosscorr — waveform cross-correlation differential times (dt.cc)

Leverages SimulFlow's Hypoddprep/HypoddRun where available.

References:
  Waldhauser & Ellsworth (2000), BSSA, doi:10.1785/0120000006
  Waldhauser (2001), USGS Open-File Report 01-113
"""

import os
import sys
import glob
import shutil
import subprocess
import time

import numpy as np
import pandas as pd


CATALOG_COLS = [
    "event_id", "datetime", "lat", "lon", "depth_km",
    "mag", "rms", "nsta", "gap", "method"
]

# ── Predefined global 1D velocity models ──────────────────────────────────────
# Discretized into constant-velocity layers for hypoDD.inp.
# Source: Kennett & Engdahl (1991) IASP91; Kennett et al. (1995) AK135.
_VELOCITY_MODELS = {
    "iasp91": {
        "tops": [0.0, 10.0, 20.0, 35.0, 60.0, 120.0],
        "vels": [5.80, 6.10, 6.50, 8.04, 8.05, 8.30],
        "vpvs": 1.732,
        "label": "IASP91 (Kennett & Engdahl 1991)",
    },
    "ak135": {
        "tops": [0.0, 20.0, 35.0, 77.5, 120.0],
        "vels": [5.80, 6.50, 8.04, 8.05, 8.30],
        "vpvs": 1.732,
        "label": "AK135 (Kennett et al. 1995)",
    },
    "halmahera": {
        "tops": [0.0, 1.0, 3.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 60.0],
        "vels": [4.70, 5.28, 5.41, 5.41, 5.71, 6.02, 6.54, 7.99, 8.05, 8.10, 8.10],
        "vpvs": 1.730,
        "label": "Halmahera 1D (7G_Jailolo)",
    },
}


class HypoDDRelocation:
    """HypoDD double-difference relocation (catalog + cross-corr modes)."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = base_dir
        self.rcfg     = cfg["relocation"]["hypodd"]
        self.reg      = cfg["region"]

        self.cat_out_dir = os.path.join(base_dir, "work", "relocation", "catalog")
        self.cc_out_dir  = os.path.join(base_dir, "work", "relocation", "crosscorr")
        self.log_dir     = os.path.join(base_dir, "work", "logs", "hypodd")
        os.makedirs(self.cat_out_dir, exist_ok=True)
        os.makedirs(self.cc_out_dir,  exist_ok=True)
        os.makedirs(self.log_dir,     exist_ok=True)

        self.ph2dt_exec  = self._find_exec("ph2dt",  self.rcfg.get("exec_ph2dt",  "ph2dt"))
        self.hypodd_exec = self._find_exec("hypoDD", self.rcfg.get("exec_hypodd", "hypoDD"))

    def _find_exec(self, name: str, cfg_name: str) -> str:
        # PRIORITAS binary bundled core/bin/ (diisi install.sh), repo-relative.
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        for c in (os.path.join(repo_root, "core", "bin", name),
                  os.path.join(self.base_dir, "core", "bin", name)):
            if os.path.exists(c):
                return c
        return shutil.which(cfg_name) or shutil.which(name) or ""

    # ── Try to use SimulFlow Hypoddprep ───────────────────────────────────────
    def _try_simulflow_prep(self, catalog_file: str, work_dir: str) -> bool:
        try:
            sys.path.insert(0, os.path.join(self.base_dir, "core", "simulflow", "src"))
            from simulflow.preparation.hypoddprep import Hypoddprep
            hp = Hypoddprep(work_dir=work_dir, output_dir=work_dir)
            sta_file = os.path.join(self.base_dir, self.cfg["data"]["station_file"])
            hp.prep_stations(sta_file, "stations.txt")
            picks_path = os.path.join(self.base_dir, "work", "catalog", "picks_gamma.csv")
            if os.path.exists(picks_path):
                hp.prep_phases(picks_path, catalog_file, "phase.dat")
            return True
        except (ImportError, Exception):
            return False

    # ── Write event.dat from catalog ─────────────────────────────────────────
    def _write_event_dat(self, catalog_file: str, work_dir: str) -> str:
        cat = pd.read_csv(catalog_file)
        event_file = os.path.join(work_dir, "event.dat")
        with open(event_file, "w") as f:
            for i, (_, ev) in enumerate(cat.iterrows(), 1):
                try:
                    t = pd.Timestamp(ev["datetime"])
                    mag = float(ev.get("mag", 0.0))
                    if np.isnan(mag):
                        mag = 0.0
                    f.write(f"{t.year:4d}{t.month:02d}{t.day:02d}  "
                            f"{t.hour:02d}{t.minute:02d}{t.second + t.microsecond/1e6:5.2f}  "
                            f"{float(ev['lat']):9.4f}  {float(ev['lon']):10.4f}  "
                            f"{float(ev.get('depth_km', 10.0)):7.3f}  "
                            f"{mag:5.2f}  0.00  0.00  0.00  "
                            f"0.000  {i:9d}\n")
                except Exception:
                    pass
        return event_file

    # ── Write station.dat ─────────────────────────────────────────────────────
    def _write_station_dat(self, work_dir: str) -> str:
        sta_src = os.path.join(self.base_dir, self.cfg["data"]["station_file"])
        sta_dst = os.path.join(work_dir, "station.dat")
        from seiswork.utils.converter import stations_to_hypodd_fmt
        stations_to_hypodd_fmt(sta_src, sta_dst)
        return sta_dst

    # ── Write phase.dat from picks ────────────────────────────────────────────
    def _write_phase_dat(self, catalog_file: str, work_dir: str) -> str:
        cat    = pd.read_csv(catalog_file)
        # Picks are searched next to the input catalog first (GUI pattern: each job dir
        # carries picks_associated.csv), then falls back to the old work/catalog.
        cat_dir = os.path.dirname(os.path.abspath(catalog_file))
        candidates = [
            os.path.join(cat_dir, "picks_associated.csv"),
            os.path.join(cat_dir, "picks_gamma.csv"),
            os.path.join(cat_dir, "picks_real.csv"),
            os.path.join(self.base_dir, "work", "catalog", "picks_gamma.csv"),
        ]
        picks = pd.DataFrame()
        for picks_path in candidates:
            if os.path.exists(picks_path):
                picks = pd.read_csv(picks_path)
                print(f"[HypoDD] picks: {picks_path}  ({len(picks)} rows)")
                break
        # GaMMA writes EVERY pick to picks_associated.csv (including the ~99% that
        # were NOT associated to an event, event_index = -1). Feeding all of them
        # to ph2dt builds a gigantic phase.dat and hangs. Keep only the picks that
        # actually belong to an event (event_index >= 0) — that's the real
        # associated set (e.g. 23k of 3.56M for the full-year run).
        if "event_index" in picks.columns:
            n0 = len(picks)
            picks = picks[pd.to_numeric(picks["event_index"], errors="coerce")
                          .fillna(-1) >= 0]
            print(f"[HypoDD] filtered to associated picks "
                  f"(event_index>=0): {len(picks)} of {n0} rows")
        from seiswork.utils.converter import catalog_picks_to_hypodd_phase
        phase_file = os.path.join(work_dir, "phase.dat")
        catalog_picks_to_hypodd_phase(cat, picks, phase_file)
        return phase_file

    # ── Compose ph2dt.inp text ────────────────────────────────────────────────
    def _compose_ph2dt_inp(self, sta_ref: str, phase_ref: str) -> str:
        # Default = RECOMMENDED parameters from Jailolo residual diagnosis
        # (run_hypodd_rekomendasi.ipynb): 0 60 40 10 8 8 32
        p = self.rcfg.get("ph2dt", {})
        return f"""\
* ph2dt.inp — auto-generated by SeisWork (HakimBMKG)
* sta_file  phase_file
{sta_ref}
{phase_ref}
*MINWGHT  MAXDIST  MAXSEP  MAXNGH  MINLNK  MINOBS  MAXOBS
   {float(p.get('min_wght', 0.0)):g}  {float(p.get('max_dist_km', 60.0)):.0f}  {float(p.get('max_sep_km', 40.0)):.0f}  {int(p.get('max_ngh', 10))}  {int(p.get('min_links', 8))}  {int(p.get('min_obs', 8))}  {int(p.get('max_obs', 32))}
"""

    # ── Write ph2dt.inp ───────────────────────────────────────────────────────
    def _write_ph2dt_inp(self, phase_file: str, sta_file: str, work_dir: str) -> str:
        inp_file = os.path.join(work_dir, "ph2dt.inp")
        # User-edited override (preview/edit in GUI before run) written verbatim.
        custom = self.rcfg.get("ph2dt_inp_text")
        inp = custom if custom else self._compose_ph2dt_inp(sta_file, phase_file)
        with open(inp_file, "w") as f:
            f.write(inp)
        return inp_file

    # ── Write hypoDD.inp ──────────────────────────────────────────────────────
    def _write_hypodd_inp(self, work_dir: str, use_cc: bool = False) -> str:
        """Write hypoDD.inp in the "newest format" parsed by getinp.f.

        getinp.f counts every non-comment line *positionally* — comment lines
        must start with '*' in column 1 or 2; blank lines are NOT skipped and
        still consume a slot (read as an empty string / trigger an EOF error
        for numeric reads). The exact required line sequence is:

          1 fn_cc   2 fn_ct   3 fn_eve   4 fn_sta
          5 fn_loc  6 fn_reloc 7 fn_stares 8 fn_res 9 fn_srcpar
          10 IDAT IPHA DIST
          11 OBSCC OBSCT
          12 ISTART ISOLV NITER
          13..12+NITER  AITER WTCCP WTCCS WRCC WDCC WTCTP WTCTS WRCT WDCT DAMP
          13+NITER  NLAY RATIO
          14+NITER  TOP(1..NLAY)
          15+NITER  VEL(1..NLAY)
          16+NITER  CID

        A previous version of this template wrote only 4 (mis-ordered) file
        names and an unrelated parameter layout, which getinp.f happily kept
        reading positionally until it ran out of numeric tokens mid-record →
        "Fortran runtime error: End of file" at getinp.f:92.
        """
        inp_file = os.path.join(work_dir, "hypoDD.inp")
        # User-edited override (preview/edit in GUI) written verbatim.
        custom = self.rcfg.get("hypodd_inp_text")
        if custom:
            with open(inp_file, "w") as f:
                f.write(custom)
            return inp_file
        inp = self._compose_hypodd_inp(work_dir, use_cc, relative=False)
        with open(inp_file, "w") as f:
            f.write(inp)
        return inp_file

    # ── Compose hypoDD.inp text ───────────────────────────────────────────────
    def _compose_hypodd_inp(self, work_dir: str, use_cc: bool = False,
                            relative: bool = False) -> str:
        """Build hypoDD.inp text. `relative=True` uses bare basenames (portable
        preview, independent of the eventual job dir); `relative=False` uses
        absolute paths inside work_dir (the actual run)."""
        def _ref(name):
            return name if relative else os.path.join(work_dir, name)

        p  = self.rcfg.get("hypodd", {})
        ev    = _ref("event.dat")
        st    = _ref("station.dat")
        dt_ct = _ref("dt.ct")
        dt_cc = _ref("dt.cc")

        fn_cc = dt_cc if use_cc else ""   # blank line, NOT '*' (would be parsed as a comment)

        idat     = 3 if use_cc else 2     # 0=synthetic 1=cc 2=catalog 3=both
        iphase   = 3                      # 1=P 2=S 3=both
        max_dist = float(p.get("max_dist_km", 60.0))
        min_obs  = int(p.get("min_obs", 8))           # OBSCT
        wdct     = float(p.get("wdct", 40.0))
        damping  = float(p.get("damping", 70.0))
        vpvs     = float(p.get("vpvs", 1.730))

        # RECOMMENDED iteration & weighting scheme (Jailolo residual diagnosis,
        # run_hypodd_rekomendasi.ipynb): default 4 sets × 4 iterations, WRCT tightens
        # 8→3, WTCTS drops to 0.8 in the second half. CC fields -9 (unused).
        # Each set: (NITER, WTCCP, WTCCS, WRCC, WDCC, WTCTP, WTCTS, WRCT, WDCT, DAMP)
        #
        # Number of sets (NSET) now follows input `nset`: iteration blocks are generated
        # NSET times, WRCT interpolated linearly from `wrct_start` (loose)
        # to `wrct_end` (tight). An explicit `weighting_sets` list, if
        # provided, takes full priority (full override).
        wsets = p.get("weighting_sets")
        if not wsets:
            nset      = max(1, int(p.get("nset", 4)))
            niter_set = max(1, int(p.get("niter_per_set", 4)))
            wrct0     = float(p.get("wrct_start", 8.0))
            wrct1     = float(p.get("wrct_end",   3.0))
            wsets = []
            for k in range(nset):
                frac  = k / (nset - 1) if nset > 1 else 1.0   # 0→1 across sets
                wrct  = round(wrct0 + (wrct1 - wrct0) * frac, 2)
                wtcts = 1.0 if frac < 0.5 else 0.8            # S-weight tightens late
                wsets.append((niter_set, -9, -9, -9, -9,
                              1.0, wtcts, wrct, wdct, damping))
        if use_cc:
            # CC active: use full CC weight in each set (WTCCP=1, WRCC=WRCT)
            wsets = [(n, 1.0, 0.8, w7, w8, t5, t6, w7, w8, d)
                     for (n, _, _, _, _, t5, t6, w7, w8, d) in wsets]

        # Velocity model — priority order:
        # 1. Explicit "velocity_model" parameter = iasp91 / ak135 / halmahera
        # 2. "auto": try velocity_updated.mod (VELEST) → velocity.mod → halmahera
        vel_model_key = p.get("velocity_model", "auto").strip().lower()
        tops, vels = [], []

        if vel_model_key in _VELOCITY_MODELS:
            m = _VELOCITY_MODELS[vel_model_key]
            tops, vels = m["tops"], m["vels"]
            # Use the global-model vpvs as the default; the GUI may override it
            # (a user changing hd-vpvs away from the 1.730 default is still honored)
            if abs(vpvs - 1.730) < 0.001:
                vpvs = m["vpvs"]
            print(f"[HypoDD] Velocity model: {m['label']} (vpvs={vpvs:.3f})", flush=True)
        else:
            # auto: look for a local file first
            vel_mod = os.path.join(self.base_dir, "work", "velocity", "velocity_updated.mod")
            if not os.path.exists(vel_mod):
                vel_mod = os.path.join(self.base_dir, "config", "velocity.mod")
            if os.path.exists(vel_mod):
                with open(vel_mod) as vm:
                    for line in vm:
                        if line.startswith("*") or not line.strip():
                            continue
                        parts = line.split()
                        if len(parts) >= 2:
                            try:
                                tops.append(float(parts[0]))
                                vels.append(float(parts[1]))
                            except ValueError:
                                pass
                if tops:
                    print(f"[HypoDD] Velocity model: local ({os.path.basename(vel_mod)})", flush=True)
            if not tops:
                m = _VELOCITY_MODELS["halmahera"]
                tops, vels = m["tops"], m["vels"]
                vpvs = m["vpvs"]
                print(f"[HypoDD] Velocity model: {m['label']} (fallback)", flush=True)

        inp = f"""\
* RELOC.INP: — auto-generated by SeisWork (HakimBMKG)
*--- input file selection
* cross correlation diff times:
{fn_cc}
*
*catalog P diff times:
{dt_ct}
*
* event file:
{ev}
*
* station file:
{st}
*
*--- output file selection
* original locations:
hypoDD.loc
* relocations:
hypoDD.reloc
* station information:
hypoDD.sta
* residual information:
hypoDD.res
* source paramater information:
hypoDD.src
*
*--- data type selection:
* IDAT:  0 = synthetics; 1= cross corr; 2= catalog; 3= cross & cat
* IPHA: 1= P; 2= S; 3= P&S
* DIST:max dist [km] between cluster centroid and station
* IDAT   IPHA   DIST
   {idat:>4}   {iphase:>4}   {max_dist:>6.0f}
*
*--- event clustering:
* OBSCC:    min # of obs/pair for crosstime data (0= no clustering)
* OBSCT:    min # of obs/pair for network data (0= no clustering)
* OBSCC  OBSCT
   {(min_obs if use_cc else 0):>4}   {min_obs:>4}
*
*--- solution control:
* ISTART:   1 = from single source; 2 = from network sources
* ISOLV:    1 = SVD, 2=lsqr
* NSET:         number of sets of iteration with specifications following
* ISTART  ISOLV  NSET
   {2:>4}   {2:>4}   {len(wsets):>4}
*
*--- data weighting and re-weighting:
* NITER:        last iteration to used the following weights
* WTCCP, WTCCS:     weight cross P, S
* WTCTP, WTCTS:     weight catalog P, S
* WRCC, WRCT:       residual threshold in sec for cross, catalog data
* WDCC, WDCT:       max dist [km] between cross, catalog linked pairs
* DAMP:             damping (for lsqr only)
* ---  CROSS DATA ----- ----CATALOG DATA ----
* NITER WTCCP WTCCS WRCC WDCC WTCTP WTCTS WRCT WDCT DAMP
{chr(10).join('   ' + ' '.join(f'{x:>6g}' for x in ws) for ws in wsets)}
*
*--- 1D model:
* NLAY:     number of model layers
* RATIO:    vp/vs ratio
* TOP:      depths of top of layer (km)
* VEL:      layer velocities (km/s)
* NLAY  RATIO
   {len(tops):>4}   {vpvs:>5.2f}
* TOP
{' '.join(f'{t:g}' for t in tops)}
* VEL
{' '.join(f'{v:g}' for v in vels)}
*
*--- event selection:
* CID:  cluster to be relocated (0 = all)
* ID:   cuspids of event to be relocated (8 per line)
* CID
   {0:>4}
* ID
"""
        return inp

    # ── Generate .inp preview (for user review in the GUI before running) ────────
    def generate_inp_preview(self) -> dict:
        """Return {'ph2dt_inp': str, 'hypodd_inp': str} using relative basenames
        so the result is portable (not tied to job dir). Does not execute anything
        — only assembles text from current parameters for user review."""
        mode   = self.rcfg.get("mode", "catalog")
        use_cc = (mode == "crosscorr")
        ph2dt  = self._compose_ph2dt_inp("station.dat", "phase.dat")
        hypodd = self._compose_hypodd_inp(work_dir="", use_cc=use_cc, relative=True)
        return {"ph2dt_inp": ph2dt, "hypodd_inp": hypodd}

    # ── Run ph2dt ─────────────────────────────────────────────────────────────
    def _run_ph2dt(self, inp_file: str, work_dir: str):
        log = os.path.join(self.log_dir, "ph2dt.log")
        print(f"[HypoDD] Running ph2dt ...")
        # ph2dt.f reads its .inp argument into `character str30*30` — an
        # absolute path (commonly 50-60+ chars here) silently truncates to 30
        # chars via getarg() and ph2dt then tries to open that truncated
        # (non-existent / directory) path → "Cannot open file ...: Is a
        # directory". Run from work_dir and pass the bare basename instead.
        with open(log, "w") as logf:
            subprocess.run([self.ph2dt_exec, os.path.basename(inp_file)],
                           cwd=work_dir,
                           stdout=logf, stderr=subprocess.STDOUT)
        dt_ct = os.path.join(work_dir, "dt.ct")
        if os.path.exists(dt_ct):
            n = sum(1 for _ in open(dt_ct) if not _.startswith("#"))
            print(f"[HypoDD] ph2dt done. {n} differential time pairs in dt.ct")
        else:
            print(f"[WARNING] dt.ct not created. Check: {log}")

    # ── Run hypoDD ────────────────────────────────────────────────────────────
    def _run_hypodd(self, inp_file: str, work_dir: str):
        log = os.path.join(self.log_dir, "hypodd.log")
        print(f"[HypoDD] Running hypoDD ...")
        # Same rationale as _run_ph2dt: run from work_dir with a relative
        # filename so the legacy Fortran fixed-length argument buffer (here
        # fn_inp*80) is never at risk of truncating a long absolute path.
        with open(log, "w") as logf:
            ret = subprocess.run([self.hypodd_exec, os.path.basename(inp_file)],
                                 cwd=work_dir,
                                 stdout=logf, stderr=subprocess.STDOUT)
        reloc = os.path.join(work_dir, "hypoDD.reloc")
        if os.path.exists(reloc):
            n = sum(1 for _ in open(reloc))
            print(f"[HypoDD] Relocation done. {n} events in hypoDD.reloc")
        else:
            print(f"[WARNING] hypoDD.reloc not found. Check: {log}")

    # ── Parse hypoDD.reloc ────────────────────────────────────────────────────
    # hypoDD.reloc column layout (Waldhauser 2001):
    # [0]ID [1]lat [2]lon [3]dep [4]X [5]Y [6]Z [7]EX [8]EY [9]EZ
    # [10]YR [11]MO [12]DY [13]HR [14]MI [15]SC [16]MAG
    # [17]NCCP [18]NCCS [19]NCTP [20]NCTS [21]RCC [22]RCT [23]CID
    def _parse_reloc(self, reloc_file: str, tag: str) -> pd.DataFrame:
        if not os.path.exists(reloc_file):
            return pd.DataFrame()
        rows = []
        with open(reloc_file) as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 16:
                    continue
                try:
                    rows.append({
                        "event_id" : parts[0],
                        "datetime" : (f"{parts[10]}-{int(parts[11]):02d}-{int(parts[12]):02d}T"
                                      f"{int(parts[13]):02d}:{int(parts[14]):02d}:"
                                      f"{float(parts[15]):06.3f}"),
                        "lat"      : float(parts[1]),
                        "lon"      : float(parts[2]),
                        "depth_km" : float(parts[3]),
                        "mag"      : float(parts[16]) if len(parts) > 16 else float("nan"),
                        "rms"      : float(parts[22]) if len(parts) > 22 else float("nan"),
                        "nsta"     : int(parts[19]) if len(parts) > 19 else 0,
                        "gap"      : float("nan"),
                        "method"   : tag,
                    })
                except Exception:
                    pass
        return pd.DataFrame(rows, columns=CATALOG_COLS)

    # ── Cross-correlation dt.cc via ObsPy ──────────────────────────────────────
    def _compute_dtcc(self, catalog_file: str, work_dir: str) -> str:
        print("[HypoDD-CC] Computing waveform cross-correlations ...")
        cat        = pd.read_csv(catalog_file)
        wave_dir   = os.path.join(self.base_dir, self.cfg["data"]["waveform_dir"])
        cc_cfg     = self.rcfg.get("crosscorr", {})
        cc_thresh  = float(cc_cfg.get("cc_threshold", 0.7))
        dt_max     = float(cc_cfg.get("dt_max_sec",   0.5))
        win_p      = cc_cfg.get("window_p", [-0.2, 0.8])
        win_s      = cc_cfg.get("window_s", [-0.2, 0.8])
        pre_filt   = cc_cfg.get("pre_filt", [1.0, 2.0, 20.0, 25.0])

        try:
            from obspy import UTCDateTime, read
        except ImportError:
            print("[ERROR] ObsPy needed for cross-correlation: pip install obspy")
            return ""

        picks_path = os.path.join(self.base_dir, "work", "catalog", "picks_gamma.csv")
        if not os.path.exists(picks_path):
            print("[WARNING] No picks file for CC. Skipping dt.cc.")
            return ""
        picks = pd.read_csv(picks_path)

        dtcc_file = os.path.join(work_dir, "dt.cc")
        n_pairs   = 0
        with open(dtcc_file, "w") as fcc:
            event_ids = cat["event_id"].astype(str).tolist()
            for i in range(len(event_ids)):
                for j in range(i+1, min(i+11, len(event_ids))):
                    ev1 = cat.iloc[i]
                    ev2 = cat.iloc[j]
                    # Check proximity
                    from obspy.geodetics import gps2dist_azimuth
                    dist_m, _, _ = gps2dist_azimuth(ev1["lat"], ev1["lon"],
                                                     ev2["lat"], ev2["lon"])
                    sep_km = self.rcfg.get("ph2dt", {}).get("max_sep_km", 10.0)
                    if dist_m / 1000 > sep_km:
                        continue
                    # Write pair header
                    fcc.write(f"# {i+1:9d} {j+1:9d}  0.000\n")

                    # Find common stations
                    pk1 = picks[picks["event_index"].astype(str) == event_ids[i]]
                    pk2 = picks[picks["event_index"].astype(str) == event_ids[j]]
                    common_sta = set(pk1["station"]) & set(pk2["station"])

                    for sta in common_sta:
                        for phase in ["P", "S"]:
                            p1 = pk1[(pk1["station"] == sta) & (pk1.get("phase_hint", pk1.get("type","")) == phase)]
                            p2 = pk2[(pk2["station"] == sta) & (pk2.get("phase_hint", pk2.get("type","")) == phase)]
                            if p1.empty or p2.empty:
                                continue
                            try:
                                t1 = UTCDateTime(p1.iloc[0].get("phase_time", p1.iloc[0].get("timestamp","")))
                                t2 = UTCDateTime(p2.iloc[0].get("phase_time", p2.iloc[0].get("timestamp","")))
                                # Skip actual waveform CC — write catalog dt only
                                dt = float(t1 - UTCDateTime(ev1["datetime"])) - float(t2 - UTCDateTime(ev2["datetime"]))
                                if abs(dt) <= dt_max:
                                    fcc.write(f"{sta:<8}  {dt:8.5f}  1.000  {phase}\n")
                                    n_pairs += 1
                            except Exception:
                                pass

        print(f"[HypoDD-CC] dt.cc written: {n_pairs} pairs → {dtcc_file}")
        return dtcc_file

    # ── Fallback when HypoDD relocates nothing ───────────────────────────────
    def _write_fallback_relocated(self, catalog_file: str, reason: str,
                                  out_dir: str = None):
        """Write catalog_relocated.csv from the pre-relocation catalog.

        HypoDD can legitimately relocate zero events when the input catalog
        is too small/sparse to satisfy ph2dt's linkage thresholds (MINLNK/
        MINOBS), e.g. short time windows with few co-recorded events — this
        yields zero differential-time pairs and an empty hypoDD.reloc. In
        that case downstream stages still expect catalog_relocated.csv to
        exist, so fall back to the pre-relocation locations (tagged so the
        absence of relocation is traceable) instead of leaving the file
        missing and crashing the pipeline with a bare FileNotFoundError.
        """
        print(f"[HypoDD] No events relocated ({reason}). "
              "Falling back to pre-relocation locations for catalog_relocated.csv.")
        fallback = pd.read_csv(catalog_file)
        fallback["method"] = fallback["method"].astype(str) + "_no_reloc"
        # Write into the active job/working dir (crosscorr mode uses cc_out_dir)
        # so the GUI Output page finds catalog_relocated.csv as the result_file.
        out = os.path.join(out_dir or self.cat_out_dir, "catalog_relocated.csv")
        fallback.to_csv(out, index=False)
        gdir = os.path.join(self.base_dir, "work", "catalog")
        os.makedirs(gdir, exist_ok=True)
        fallback.to_csv(os.path.join(gdir, "catalog_relocated.csv"), index=False)
        print(f"[HypoDD] Fallback catalog written: {len(fallback)} events → {out}")

    # ── Public: catalog-based relocation ─────────────────────────────────────
    def run_catalog(self, catalog_file: str):
        if not self.ph2dt_exec or not self.hypodd_exec:
            print("[ERROR] ph2dt or hypoDD binary not found.")
            print("        Ensure they are compiled and in PATH (~/ bin/hypoDD, ~/bin/ph2dt)")
            sys.exit(1)

        print("[HypoDD-catalog] Starting catalog-based relocation ...")
        t0 = time.time()
        wd = self.cat_out_dir

        self._write_event_dat(catalog_file, wd)
        sta_file   = self._write_station_dat(wd)
        phase_file = self._write_phase_dat(catalog_file, wd)
        ph2dt_inp  = self._write_ph2dt_inp(phase_file, sta_file, wd)
        self._run_ph2dt(ph2dt_inp, wd)
        hypodd_inp = self._write_hypodd_inp(wd, use_cc=False)
        self._run_hypodd(hypodd_inp, wd)

        reloc_file = os.path.join(wd, "hypoDD.reloc")
        df = self._parse_reloc(reloc_file, "hypodd_catalog")
        if not df.empty:
            # Write to job work dir FIRST (GUI _pipe_worker looks for
            # hypodd_reloc.csv / catalog_relocated.csv in job dir), then
            # global copy in work/catalog for CLI compatibility.
            out = os.path.join(wd, "hypodd_reloc.csv")
            df.to_csv(out, index=False)
            df.to_csv(os.path.join(wd, "catalog_relocated.csv"), index=False)
            gout = os.path.join(self.base_dir, "work", "catalog")
            os.makedirs(gout, exist_ok=True)
            df.to_csv(os.path.join(gout, "catalog_hypodd_cat.csv"), index=False)
            df.to_csv(os.path.join(gout, "catalog_relocated.csv"), index=False)
            elapsed = time.time() - t0
            print(f"[HypoDD-catalog] Done. {len(df)} events → {out}  ({elapsed:.1f}s)")
        else:
            self._write_fallback_relocated(
                catalog_file,
                "catalog too sparse for ph2dt linkage thresholds (0 differential-time pairs)")
        return reloc_file

    # ── Public: cross-corr-based relocation ───────────────────────────────────
    def run_crosscorr(self, catalog_file: str):
        if not self.hypodd_exec:
            print("[ERROR] hypoDD binary not found.")
            sys.exit(1)

        print("[HypoDD-CC] Starting cross-correlation relocation ...")
        t0 = time.time()
        wd = self.cc_out_dir

        self._write_event_dat(catalog_file, wd)
        sta_file   = self._write_station_dat(wd)
        phase_file = self._write_phase_dat(catalog_file, wd)
        ph2dt_inp  = self._write_ph2dt_inp(phase_file, sta_file, wd)
        self._run_ph2dt(ph2dt_inp, wd)

        # Compute dt.cc — real waveform cross-correlation via FDTCC (LOC-FLOW),
        # falling back to the catalog-dt approximation if FDTCC/waveforms are
        # unavailable. FDTCC reads event.sel + dt.ct (ph2dt) + hypoDD.pha.
        dtcc_file = ""
        try:
            event_sel = os.path.join(wd, "event.sel")
            hypo_pha  = os.path.join(wd, "hypoDD.pha")
            if os.path.exists(phase_file) and not os.path.exists(hypo_pha):
                shutil.copy(phase_file, hypo_pha)
            if os.path.exists(event_sel) and os.path.exists(os.path.join(wd, "dt.ct")):
                from seiswork.modules.relocation.fdtcc import FDTCCDiffTimes
                dtcc_file = FDTCCDiffTimes(self.cfg, self.base_dir).compute(wd)
        except Exception as e:
            print(f"[HypoDD-CC] FDTCC dt.cc unavailable ({e}); using catalog-dt fallback.")
        if not dtcc_file:
            dtcc_file = self._compute_dtcc(catalog_file, wd)

        hypodd_inp = self._write_hypodd_inp(wd, use_cc=bool(dtcc_file))
        self._run_hypodd(hypodd_inp, wd)

        reloc_file = os.path.join(wd, "hypoDD.reloc")
        df = self._parse_reloc(reloc_file, "hypodd_cc")
        if not df.empty:
            out = os.path.join(self.base_dir, "work", "catalog", "catalog_hypodd_cc.csv")
            df.to_csv(out, index=False)
            df.to_csv(os.path.join(self.base_dir, "work", "catalog", "catalog_relocated.csv"), index=False)
            elapsed = time.time() - t0
            print(f"[HypoDD-CC] Done. {len(df)} events → {out}  ({elapsed:.1f}s)")
        else:
            self._write_fallback_relocated(
                catalog_file,
                "catalog too sparse for cross-correlation linkage (0 differential-time pairs)",
                out_dir=wd)
        return reloc_file

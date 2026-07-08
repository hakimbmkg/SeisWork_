#!/usr/bin/env python3
"""
SeisWork — GrowClust relative relocation module
Author : HakimBMKG

GrowClust (Trugman & Shearer, 2017) is the final relative-location refinement
stage in the LOC-FLOW workflow (Zhang et al., 2022). It clusters events with a
hierarchical (grow) algorithm using differential travel times and relocates
each cluster relative to its own centroid — more robust to outliers than the
damped least-squares of hypoDD.

In LOC-FLOW GrowClust consumes the hypoDD_dtct outputs (relocated events +
dt.cc from FDTCC). Here we keep the same alur but reuse the catalog
differential times (dt.ct) already produced for hypoDD: dt.ct is converted to
GrowClust's single-difference dt.cc format (dt = tt1 - tt2, tdif_fmt = 12).
When real waveform cross-correlation times are available they can be dropped in
as dt.cc unchanged.

References:
  Trugman, D. T., and P. M. Shearer (2017), SRL, doi:10.1785/0220160188
  Zhang, M., et al. (2022), SRL, doi:10.1785/0220220019  (LOC-FLOW)
"""

import os
import sys
import shutil
import subprocess
import time

import numpy as np
import pandas as pd

from seiswork.modules.relocation.hypodd import HypoDDRelocation, CATALOG_COLS


class GrowClustRelocation:
    """GrowClust relative relocation, driven from the hypoDD-style inputs."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = base_dir
        self.rcfg     = cfg["relocation"].get("growclust", {})
        # Reuse the hypoDD preparation helpers (event.dat / station.dat /
        # phase.dat / ph2dt → dt.ct) so the two relative-location stages share
        # exactly the same inputs, matching the LOC-FLOW alur.
        self._hd = HypoDDRelocation(cfg, base_dir)

        self.out_dir = os.path.join(base_dir, "work", "relocation", "growclust")
        self.log_dir = os.path.join(base_dir, "work", "logs", "growclust")
        os.makedirs(self.out_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        for sub in ("IN", "OUT", "TT"):
            os.makedirs(os.path.join(self.out_dir, sub), exist_ok=True)

        self.gc_exec = self._find_exec()

    def _find_exec(self) -> str:
        name = self.rcfg.get("exec", "growclust")
        found = shutil.which(name) or shutil.which("growclust")
        if not found:
            for c in [os.path.join(os.path.expanduser("~"), "bin", "growclust"),
                      os.path.join(self.base_dir, "core", "bin", "growclust")]:
                if os.path.exists(c):
                    return c
        return found or ""

    # ── station.dat → stlist.txt  (sta lat lon) ───────────────────────────────
    def _write_stlist(self, station_dat: str, in_dir: str) -> str:
        out = os.path.join(in_dir, "stlist.txt")
        with open(station_dat) as f, open(out, "w") as g:
            for line in f:
                p = line.split()
                if len(p) >= 3:
                    g.write(f"{p[0]} {float(p[1]):.6f} {float(p[2]):.6f}\n")
        return out

    # ── catalog → evlist.txt  (GrowClust evlist_fmt = 0) ──────────────────────
    def _write_evlist(self, catalog_file: str, in_dir: str) -> str:
        """evlist fmt 0: yr mo dy hr mn sec lat lon dep mag eh ez rms evid.

        The event id is the 1-based row index, matching how _write_event_dat
        numbers events for ph2dt/hypoDD so dt.ct ids line up.
        """
        cat = pd.read_csv(catalog_file)
        out = os.path.join(in_dir, "evlist.txt")
        with open(out, "w") as f:
            for i, (_, ev) in enumerate(cat.iterrows(), 1):
                try:
                    t   = pd.Timestamp(ev["datetime"])
                    mag = float(ev.get("mag", 0.0))
                    if np.isnan(mag):
                        mag = 0.0
                    sec = t.second + t.microsecond / 1e6
                    f.write(f"{t.year} {t.month} {t.day} {t.hour} {t.minute} "
                            f"{sec:.2f} {float(ev['lat']):.6f} {float(ev['lon']):.6f} "
                            f"{float(ev.get('depth_km', 10.0)):.3f} {mag:.2f} "
                            f"0.0 0.0 {float(ev.get('rms', 0.0)):.2f} {i}\n")
                except Exception:
                    pass
        return out

    # ── velocity model → vzmodel.txt  (depth vp vs) ───────────────────────────
    def _write_vzmodel(self, in_dir: str) -> str:
        tops, vels = self._hd_velocity()
        vpvs = float(self.rcfg.get("vpvs", self._hd.rcfg.get("hypodd", {}).get("vpvs", 1.73)))
        out = os.path.join(in_dir, "vzmodel.txt")
        with open(out, "w") as f:
            for t, v in zip(tops, vels):
                f.write(f"{t:7.2f}   {v:5.2f}  {v / vpvs:5.2f}\n")
        return out

    def _hd_velocity(self):
        """Reuse hypoDD's velocity-model resolution (VELEST update → config →
        Halmahera default)."""
        vel_mod = os.path.join(self.base_dir, "work", "velocity", "velocity_updated.mod")
        if not os.path.exists(vel_mod):
            vel_mod = os.path.join(self.base_dir, "config", "velocity.mod")
        tops, vels = [], []
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
        if not tops:
            tops = [0.0, 1.0, 3.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 60.0]
            vels = [4.70, 5.28, 5.41, 5.41, 5.71, 6.02, 6.54, 7.99, 8.05, 8.10, 8.10]
        return tops, vels

    # ── dt.ct (hypoDD) → dt.cc (GrowClust) ────────────────────────────────────
    def _dtct_to_dtcc(self, dt_ct: str, in_dir: str) -> str:
        """Convert hypoDD catalog differential times to GrowClust dt.cc.

        hypoDD dt.ct line:  STA  tt1  tt2  weight  PHASE   (two absolute tt's)
        GrowClust dt.cc line: STA  dt   weight  PHASE      (single difference)
        with tdif_fmt = 12 → dt = tt1 - tt2. Pair header keeps the otc field.
        """
        out   = os.path.join(in_dir, "dt.cc")
        npair = nobs = 0
        with open(dt_ct) as f, open(out, "w") as g:
            for line in f:
                if line.startswith("#"):
                    p = line.split()
                    # "# id1 id2"  →  "# id1 id2 0.0"  (origin-time correction)
                    g.write(f"# {p[1]} {p[2]} 0.0\n")
                    npair += 1
                else:
                    p = line.split()
                    if len(p) >= 5:
                        sta, tt1, tt2, wgt, pha = p[0], float(p[1]), float(p[2]), p[3], p[4]
                        g.write(f"{sta} {tt1 - tt2:.4f} {wgt} {pha}\n")
                        nobs += 1
        print(f"[GrowClust] dt.cc from dt.ct: {npair} pairs, {nobs} obs → {out}")
        return out

    # ── growclust.inp control file ────────────────────────────────────────────
    def _write_growclust_inp(self, in_dir: str, tt_dir: str, out_dir: str) -> str:
        g = self.rcfg
        rmin    = float(g.get("rmin",      0.6))
        delmax  = float(g.get("delmax",    120))
        rmsmax  = float(g.get("rmsmax",    0.2))
        rmincut = float(g.get("rmincut",   0.0))
        ngoodmin= int(g.get("ngoodmin",    8))
        iponly  = int(g.get("iponly",      0))
        nboot   = int(g.get("nboot",       0))
        nbranch = int(g.get("nbranch_min", 1))
        maxdep  = float(g.get("tt_dep1",   40.0))
        maxdel  = float(g.get("tt_del1",   200.0))

        inp = f"""\
* GrowClust control file — auto-generated by SeisWork (HakimBMKG)
*** Event list ***
* evlist_fmt (0 = evlist[fixed-width], 1 = phase[free-format], 2 = GrowClust, 3 = HypoInverse)
* We write a free-format "yr mo dy hr mn sec lat lon dep mag eh ez rms evid"
* event list (same as LOC-FLOW), which is the phase format → evlist_fmt = 1.
1
* fin_evlist
{os.path.join(in_dir, 'evlist.txt')}
*** Station list ***
* stlist_fmt (0 = SEED channel, 1 = station name)
1
* fin_stlist
{os.path.join(in_dir, 'stlist.txt')}
*** XCOR data ***
* xcordat_fmt (0 = binary, 1 = text), tdif_fmt (21 = tt2-tt1, 12 = tt1-tt2)
1  12
* fin_xcordat
{os.path.join(in_dir, 'dt.cc')}
*** Velocity / travel-time tables ***
* fin_vzmdl
{os.path.join(in_dir, 'vzmodel.txt')}
* fout_vzfine
{os.path.join(tt_dir, 'vzfine.txt')}
* fout_pTT
{os.path.join(tt_dir, 'tt.pg')}
* fout_sTT
{os.path.join(tt_dir, 'tt.sg')}
*** Travel-time table parameters ***
* vpvs_factor  rayparam_min (-1 = default)
  -1            -1
* tt_dep0  tt_dep1  tt_ddep
   0.        {maxdep:.1f}      1.0
* tt_del0  tt_del1  tt_ddel
   0.        {maxdel:.1f}     1.0
*** GrowClust algorithm parameters ***
* rmin  delmax rmsmax
  {rmin:g}    {delmax:g}    {rmsmax:g}
* rpsavgmin, rmincut  ngoodmin   iponly
   0          {rmincut:g}       {ngoodmin}        {iponly}
*** Output files ***
* nboot  nbranch_min
   {nboot}         {nbranch}
* fout_cat
{os.path.join(out_dir, 'out.growclust_cat')}
* fout_clust
{os.path.join(out_dir, 'out.growclust_clust')}
* fout_log
{os.path.join(out_dir, 'out.growclust_log')}
* fout_boot
{os.path.join(out_dir, 'out.growclust_boot')}
"""
        inp_file = os.path.join(self.out_dir, "growclust.inp")
        with open(inp_file, "w") as f:
            f.write(inp)
        return inp_file

    # ── Run growclust ─────────────────────────────────────────────────────────
    def _run_growclust(self, inp_file: str):
        log = os.path.join(self.log_dir, "growclust.log")
        print("[GrowClust] Running growclust ...")
        with open(log, "w") as logf:
            subprocess.run([self.gc_exec, os.path.basename(inp_file)],
                           cwd=self.out_dir, stdout=logf, stderr=subprocess.STDOUT)

    # ── Parse out.growclust_cat ───────────────────────────────────────────────
    def _parse_cat(self, cat_file: str, tag: str) -> pd.DataFrame:
        """GrowClust cat columns:
          0 yr 1 mo 2 dy 3 hr 4 mn 5 sec 6 evid 7 latR 8 lonR 9 depR 10 mag
          11 qID 12 cID 13 nbranch 14 qnpair 15 qndiffP 16 qndiffS
          17 rmsP 18 rmsS 19 eh 20 ez 21 et 22 latC 23 lonC 24 depC
        """
        if not os.path.exists(cat_file):
            return pd.DataFrame()
        rows = []
        with open(cat_file) as f:
            for line in f:
                p = line.split()
                if len(p) < 11:
                    continue
                try:
                    rows.append({
                        "event_id": p[6],
                        "datetime": f"{int(p[0]):04d}-{int(p[1]):02d}-{int(p[2]):02d}T"
                                    f"{int(p[3]):02d}:{int(p[4]):02d}:{float(p[5]):06.3f}",
                        "lat"     : float(p[7]),
                        "lon"     : float(p[8]),
                        "depth_km": float(p[9]),
                        "mag"     : float(p[10]),
                        "rms"     : float(p[17]) if len(p) > 17 else float("nan"),
                        "nsta"    : int(p[13]) if len(p) > 13 else 0,
                        "gap"     : float("nan"),
                        "method"  : tag,
                    })
                except Exception:
                    pass
        return pd.DataFrame(rows, columns=CATALOG_COLS)

    # ── Public: catalog-based relative relocation ─────────────────────────────
    def run_catalog(self, catalog_file: str):
        if not self.gc_exec:
            print("[ERROR] growclust binary not found "
                  "(expected in PATH, ~/bin/growclust, or core/bin/growclust).")
            sys.exit(1)

        print("[GrowClust] Starting catalog-based relative relocation ...")
        t0 = time.time()
        in_dir  = os.path.join(self.out_dir, "IN")
        tt_dir  = os.path.join(self.out_dir, "TT")
        gout_dir= os.path.join(self.out_dir, "OUT")

        # 1) Prepare the shared hypoDD-style inputs (event.dat/station.dat/dt.ct)
        wd = in_dir
        self._hd.cat_out_dir = wd
        self._hd._write_event_dat(catalog_file, wd)
        station_dat = self._hd._write_station_dat(wd)
        phase_file  = self._hd._write_phase_dat(catalog_file, wd)
        ph2dt_inp   = self._hd._write_ph2dt_inp(phase_file, station_dat, wd)
        self._hd._run_ph2dt(ph2dt_inp, wd)
        dt_ct = os.path.join(wd, "dt.ct")
        if not os.path.exists(dt_ct):
            print("[GrowClust] dt.ct not produced — catalog too sparse. Falling back.")
            self._fallback(catalog_file)
            return

        # 2) Convert to GrowClust inputs
        self._write_stlist(station_dat, in_dir)
        self._write_evlist(catalog_file, in_dir)
        self._write_vzmodel(in_dir)
        # dt.cc: prefer real waveform cross-correlation from FDTCC (LOC-FLOW);
        # FDTCC's dt.cc is already in GrowClust's single-difference format. Fall
        # back to catalog differential times (dt.ct → dt.cc) if FDTCC/waveforms
        # are unavailable.
        used_fdtcc = False
        if self.rcfg.get("use_fdtcc", True):
            try:
                event_sel = os.path.join(in_dir, "event.sel")
                hypo_pha  = os.path.join(in_dir, "hypoDD.pha")
                phase_dat = os.path.join(in_dir, "phase.dat")
                if os.path.exists(phase_dat) and not os.path.exists(hypo_pha):
                    import shutil as _sh; _sh.copy(phase_dat, hypo_pha)
                if os.path.exists(event_sel):
                    from seiswork.modules.relocation.fdtcc import FDTCCDiffTimes
                    cc = FDTCCDiffTimes(self.cfg, self.base_dir).compute(in_dir)
                    if cc:
                        import shutil as _sh
                        _sh.copy(cc, os.path.join(in_dir, "dt.cc"))
                        used_fdtcc = True
            except Exception as e:
                print(f"[GrowClust] FDTCC dt.cc unavailable ({e}); using catalog dt.")
        if not used_fdtcc:
            self._dtct_to_dtcc(dt_ct, in_dir)
        else:
            print("[GrowClust] Using real FDTCC waveform-CC dt.cc.")
        inp = self._write_growclust_inp(in_dir, tt_dir, gout_dir)

        # 3) Run
        self._run_growclust(inp)

        cat_file = os.path.join(gout_dir, "out.growclust_cat")
        df = self._parse_cat(cat_file, "growclust")
        if not df.empty:
            df.to_csv(os.path.join(self.out_dir, "growclust_reloc.csv"), index=False)
            df.to_csv(os.path.join(self.out_dir, "catalog_relocated.csv"), index=False)
            gdir = os.path.join(self.base_dir, "work", "catalog")
            os.makedirs(gdir, exist_ok=True)
            df.to_csv(os.path.join(gdir, "catalog_growclust.csv"), index=False)
            df.to_csv(os.path.join(gdir, "catalog_relocated.csv"), index=False)
            print(f"[GrowClust] Done. {len(df)} events relocated  ({time.time()-t0:.1f}s)")
        else:
            self._fallback(catalog_file)
        return cat_file

    def _fallback(self, catalog_file: str):
        print("[GrowClust] No events relocated — falling back to input locations.")
        df = pd.read_csv(catalog_file)
        df["method"] = df["method"].astype(str) + "_no_reloc"
        df.to_csv(os.path.join(self.out_dir, "catalog_relocated.csv"), index=False)
        gdir = os.path.join(self.base_dir, "work", "catalog")
        os.makedirs(gdir, exist_ok=True)
        df.to_csv(os.path.join(gdir, "catalog_relocated.csv"), index=False)

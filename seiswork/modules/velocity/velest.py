#!/usr/bin/env python3
"""
SeisWork — VELEST velocity module — by HakimBMKG

Pipeline (adapted from SimulFlow/7G_Jailolo.ipynb):
  1. catalog CSV + picks CSV → intermediate phase file (# header format)
  2. Phase2velest.convert()  → velest_input.pha (VELEST format)
  3. Velestprep:
       import_pha()           → velest.pha
       stations2veleststa()   → velest.sta
       model2velestmod()      → velest.mod
       make_cmn()             → velest.cmn
  4. VelestRun.run()

Reference:
  Kissling et al. (1994), JGR, doi:10.1029/93JB03138
  https://seg.ethz.ch/software/velest.html
"""

import os
import sys
import shutil
import time
from pathlib import Path

import pandas as pd


# ── Halmahera/Jailolo 1D initial velocity model (from 7G_Jailolo.ipynb) ──────
MODEL_HALMAHERA = [
    {'depth': -3.00, 'vp': 4.70},
    {'depth':  0.00, 'vp': 4.70},
    {'depth':  1.00, 'vp': 5.50},
    {'depth':  3.00, 'vp': 5.60},
    {'depth':  5.00, 'vp': 5.70},
    {'depth': 10.00, 'vp': 5.80},
    {'depth': 15.00, 'vp': 6.00},
    {'depth': 20.00, 'vp': 6.50},
    {'depth': 25.00, 'vp': 8.00},
    {'depth': 30.00, 'vp': 8.05},
    {'depth': 40.00, 'vp': 8.10},
    {'depth': 60.00, 'vp': 8.08},
]


class VelestVelocity:
    """Prepare and run VELEST to derive 1D local velocity model."""

    def __init__(self, cfg: dict, base_dir):
        self.cfg      = cfg
        self.base_dir = str(base_dir)
        self.vcfg     = cfg.get("velocity", {}).get("velest", {})
        self.reg      = cfg.get("region", {})

        self.vel_dir = os.path.join(self.base_dir, "work", "velocity")
        os.makedirs(self.vel_dir, exist_ok=True)

        # SimulFlow path
        self.simulflow_src = os.path.join(
            self.base_dir, "core", "simulflow", "src")
        if not os.path.isdir(self.simulflow_src):
            self.simulflow_src = os.path.join(
                os.path.expanduser("~"), "apps", "simulflow", "src")

        sys.path.insert(0, self.simulflow_src)

    # ── Find VELEST binary ────────────────────────────────────────────────────
    def _find_velest(self) -> str:
        candidates = [
            self.vcfg.get("exec", ""),
            os.path.join(self.simulflow_src, "simulflow", "bin", "velest", "velest"),
            os.path.join(os.path.expanduser("~"), "apps", "simulflow",
                         "src", "simulflow", "bin", "velest", "velest"),
            shutil.which("velest") or "",
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c
        return ""

    # ── Generate intermediate phase file from catalog + picks CSV ─────────────
    def _build_phase_file(self, catalog_csv: str, picks_csv: str,
                          out_pha: str) -> int:
        """
        Generate phase file in the format:
          # YYYY MM DD HH MM SS.mmm lat lon depth mag rms 0.00 0.00 nsta
           STATION  travel_time weight phase
        (same as pipeline_job_phase_text / Newcatalog_magPhases.txt)
        Picks are matched to events using time-window (most robust across
        different association methods: GaMMA, REAL, etc.).
        """
        from datetime import datetime as _dt

        cat_df  = pd.read_csv(catalog_csv)
        pick_df = pd.read_csv(picks_csv) if picks_csv and Path(picks_csv).exists() else pd.DataFrame()
        # GaMMA's picks_associated.csv carries EVERY pick (the ~99% with
        # event_index = -1 were never associated). Keep only associated picks so
        # the catalog↔pick join stays small and fast (else millions of rows).
        if not pick_df.empty and "event_index" in pick_df.columns:
            _n0 = len(pick_df)
            pick_df = pick_df[pd.to_numeric(pick_df["event_index"],
                                            errors="coerce").fillna(-1) >= 0]
            print(f"[VELEST] filtered to associated picks "
                  f"(event_index>=0): {len(pick_df)} of {_n0}")

        # Pre-parse all pick timestamps once for efficiency
        pick_times = []
        ts_col_used = None
        if not pick_df.empty:
            for col in ("timestamp", "pick_time", "phase_time"):
                if col in pick_df.columns:
                    ts_col_used = col
                    break
            if ts_col_used:
                for v in pick_df[ts_col_used]:
                    try:
                        pick_times.append(_dt.fromisoformat(
                            str(v).replace("Z", "").split("+")[0]))
                    except Exception:
                        pick_times.append(None)
            else:
                pick_times = [None] * len(pick_df)

        lines  = []
        n_evs  = 0
        for _, ev in cat_df.iterrows():
            ot_str = str(ev.get("datetime", ""))
            try:
                ot = _dt.fromisoformat(ot_str.replace("Z", "").split("+")[0])
            except Exception:
                continue

            lat   = float(ev.get("lat",  0) or 0)
            lon   = float(ev.get("lon",  0) or 0)
            dep   = float(ev.get("depth_km", ev.get("depth", 0)) or 0)
            mag   = float(ev.get("mag",  0) or 0)
            nsta  = int(ev.get("nsta",   0) or 0)
            rms   = float(ev.get("rms",  0) or 0)

            lines.append(
                f"# {ot.year:4d} {ot.month:02d} {ot.day:02d} "
                f"{ot.hour:02d} {ot.minute:02d} {ot.second:02d}."
                f"{ot.microsecond // 1000:03d} "
                f"{lat:.4f} {lon:.4f} {dep:.3f} {mag:.2f} "
                f"{rms:.2f} 0.00 0.00 {nsta}"
            )

            # Time-window match: pick_time in (ot, ot+200s)
            # Collect per (station, phase) → keep shortest travel-time only
            # to avoid PHASETEST duplicate-pick errors in VELEST.
            best: dict[tuple, float] = {}
            for i, pt in enumerate(pick_times):
                if pt is None:
                    continue
                tt = (pt - ot).total_seconds()
                if not (0 < tt < 200):
                    continue
                pk = pick_df.iloc[i]
                sta   = str(pk.get("station", "")).strip()
                phase = str(pk.get("type",
                            pk.get("phase_hint",
                            pk.get("phase", "P")))).upper()
                if phase not in ("P", "S") or not sta:
                    continue
                key = (sta, phase)
                if key not in best or tt < best[key]:
                    best[key] = tt

            has_picks = False
            for (sta, phase), tt in best.items():
                lines.append(f" {sta:<8s} {round(tt, 3):7.3f} 1 {phase}")
                has_picks = True

            if has_picks:
                n_evs += 1

        Path(out_pha).write_text("\n".join(lines) + "\n")
        print(f"[VELEST] Phase file: {out_pha}  ({n_evs} events with picks)")
        return n_evs

    # ── Quality filter (azimuthal gap / mag / min phases) ─────────────────────
    def _apply_quality_filter(self, pha_file: str) -> int:
        """Drop poorly-constrained events before VELEST, reproducing the
        notebook's filter_pha (max_gap, min_mag, min_phase). Azimuthal gap is
        computed from the recording stations' geometry. Returns kept events.

        Without this, far-azimuth / few-phase events inflate the RMS (the
        reference 7G_Jailolo run filtered max_gap=220 → 2067 → 935 events).
        """
        import math
        max_gap   = float(self.vcfg.get("filter_max_gap",   0) or 0)
        min_mag   = float(self.vcfg.get("filter_min_mag",   0) or 0)
        min_phase = int(self.vcfg.get("filter_min_phase",   0) or 0)
        if max_gap <= 0 and min_mag <= 0 and min_phase <= 0:
            return -1   # no filtering requested

        # station coords
        sta = {}
        sta_src = self._find_station_file()
        if sta_src and os.path.exists(sta_src):
            try:
                from seiswork.utils.converter import _load_station_df
                for _, r in _load_station_df(sta_src).iterrows():
                    sta[str(r["station"])] = (float(r["lat"]), float(r["lon"]))
            except Exception:
                pass

        def azgap(la, lo, stations):
            az = []
            for s in stations:
                if s not in sta:
                    continue
                sla, slo = sta[s]
                dlon = math.radians(slo - lo)
                y = math.sin(dlon) * math.cos(math.radians(sla))
                x = (math.cos(math.radians(la)) * math.sin(math.radians(sla)) -
                     math.sin(math.radians(la)) * math.cos(math.radians(sla)) * math.cos(dlon))
                az.append((math.degrees(math.atan2(y, x)) + 360) % 360)
            if len(az) < 2:
                return 360.0
            az = sorted(set(round(a, 3) for a in az))
            gaps = [az[i + 1] - az[i] for i in range(len(az) - 1)] + [360 - az[-1] + az[0]]
            return max(gaps)

        # parse events
        events, cur = [], None
        for ln in open(pha_file):
            if ln.startswith("#"):
                if cur:
                    events.append(cur)
                p = ln.split()
                try:
                    cur = {"hdr": ln, "lat": float(p[7]), "lon": float(p[8]),
                           "mag": float(p[10]), "stations": set(), "lines": []}
                except (ValueError, IndexError):
                    cur = None
            elif ln.strip() and cur is not None:
                cur["lines"].append(ln)
                cur["stations"].add(ln.split()[0])
        if cur:
            events.append(cur)

        kept = 0
        with open(pha_file, "w") as g:
            for e in events:
                if min_mag and e["mag"] < min_mag:
                    continue
                if min_phase and len(e["lines"]) < min_phase:
                    continue
                if max_gap and sta and azgap(e["lat"], e["lon"], e["stations"]) > max_gap:
                    continue
                g.write(e["hdr"])
                for l in e["lines"]:
                    g.write(l)
                kept += 1
        print(f"[VELEST] Quality filter (gap<={max_gap}, mag>={min_mag}, "
              f"nph>={min_phase}): {len(events)} → {kept} events")
        return kept

    # ── Find picks CSV accompanying the catalog ───────────────────────────────
    def _find_picks(self, catalog_csv: str) -> str:
        search_dirs = [Path(self.vel_dir)]
        cat_dir = Path(catalog_csv).resolve().parent
        if cat_dir != Path(self.vel_dir):
            search_dirs.append(cat_dir)
        for d in search_dirs:
            for nm in ("picks_associated.csv", "picks_gamma.csv",
                       "picks_real.csv", "picks_nlloc.csv"):
                p = d / nm
                if p.exists():
                    return str(p)
        return ""

    # ── Find station file ─────────────────────────────────────────────────────
    def _find_station_file(self) -> str:
        sta_cfg = self.cfg.get("data", {}).get("station_file", "")
        if sta_cfg:
            p = Path(sta_cfg)
            if not p.is_absolute():
                p = Path(self.base_dir) / p
            if p.exists():
                return str(p)

        # Common fallback locations
        home = Path(os.path.expanduser("~"))
        fallbacks = [
            Path(self.base_dir) / "station.txt",
            Path(self.base_dir) / "7gsta.txt",
            Path(self.base_dir) / "7gSta.txt",
            Path(self.base_dir) / "stations.txt",
            home / "apps" / "DATA_7G" / "7gSta.txt",
            home / "apps" / "DATA_7G" / "7gsta.txt",
            home / "apps" / "simulflow" / "7gsta.txt",
            home / "apps" / "simulflow" / "7gSta.txt",
            home / "Disertasi_DATA" / "JAILOLO" / "7gsta.txt",
            home / "Disertasi_DATA" / "JAILOLO" / "stations7GTnti.txt",
        ]
        for p in fallbacks:
            if p.exists():
                return str(p)
        return ""

    # ── Convert station CSV/TXT to VELEST sta format using SimulFlow ──────────
    def _stations2velest_sta(self, sta_src: str, dst: str):
        try:
            from simulflow.preparation.velestprep import Velestprep
            vp = Velestprep(output_dir=self.vel_dir)
            vp.stations2veleststa(sta_src, output_filename=os.path.basename(dst))
            print(f"[VELEST] Station file: {dst}")
        except ImportError:
            # Fallback: use SeisWork's own converter
            from seiswork.utils.converter import stations_to_velest_sta
            stations_to_velest_sta(sta_src, dst)
            print(f"[VELEST] Station file (fallback): {dst}")

    # ── Write initial velocity model ──────────────────────────────────────────
    def _write_velest_mod(self, dst: str):
        # Check for user-supplied model file
        src = self.vcfg.get("initial_model", "")
        if src:
            p = Path(src) if Path(src).is_absolute() else Path(self.base_dir) / src
            if p.is_file():
                shutil.copy2(str(p), dst)
                print(f"[VELEST] Initial model copied: {p}")
                return

        # Generate from Halmahera layers using SimulFlow
        try:
            from simulflow.preparation.velestprep import Velestprep
            vp = Velestprep(output_dir=self.vel_dir)
            vp.model2velestmod(
                layers=MODEL_HALMAHERA,
                output_filename=os.path.basename(dst),
                title="Halmahera 1D Velocity (SeisWork auto)"
            )
            print(f"[VELEST] Initial model written (Halmahera 1D): {dst}")
        except ImportError:
            self._write_default_mod_fallback(dst)

    def _write_default_mod_fallback(self, path: str):
        """Manual fallback without SimulFlow: Halmahera 1D in VELEST format."""
        layers = MODEL_HALMAHERA
        n = len(layers)
        lines = [f"Halmahera 1D Velocity (SeisWork auto)"]
        lines.append(f"{n:3d}        vel,depth,vdamp,phase (f5.2,5x,f7.2,2x,f7.3,3x,a1)")
        for i, L in enumerate(layers):
            sfx = "   P-VELOCITY MODEL" if i == 0 else ""
            lines.append(f"{L['vp']:5.2f}     {L['depth']:7.2f}  1.000{sfx}")
        lines.append(f"{n:3d}")
        vpvs = 1.730
        for i, L in enumerate(layers):
            vs = L['vp'] / vpvs
            sfx = "   S-VELOCITY MODEL" if i == 0 else ""
            lines.append(f"{vs:5.2f}     {L['depth']:7.2f}  1.000{sfx}")
        Path(path).write_text("\n".join(lines) + "\n")
        print(f"[VELEST] Initial model written (fallback): {path}")

    # ── Copy static VELEST region files ──────────────────────────────────────
    def _copy_static_files(self):
        """Copy regionskoord.dat / regionsnamen.dat to vel_dir."""
        try:
            src_dir = Path(self.simulflow_src) / "simulflow" / "bin" / "velest"
            for fn in ("regionskoord.dat", "regionsnamen.dat"):
                src = src_dir / fn
                dst = Path(self.vel_dir) / fn
                if src.exists() and not dst.exists():
                    shutil.copy2(str(src), str(dst))
        except Exception:
            pass

    # ── Write velest.cmn using SimulFlow's Velestprep.make_cmn() ─────────────
    def _write_cmn(self, neqs: int, mode: int) -> str:
        lat_ref  = self.vcfg.get("lat_ref",  self.reg.get("lat",  float((self.reg.get("lat_min", 0) + self.reg.get("lat_max", 2)) / 2)))
        lon_ref  = self.vcfg.get("lon_ref",  self.reg.get("lon",  float((self.reg.get("lon_min", 127) + self.reg.get("lon_max", 128)) / 2)))
        distmax  = self.vcfg.get("distmax",  100.0)   # ref 7G_Jailolo (1000 → RMS rises)
        vpvs     = self.vcfg.get("vpvs_ratio", 1.730)
        vthet    = self.vcfg.get("damping_vel", 10)
        stathet  = self.vcfg.get("damping_sta",  1)
        zmin     = self.vcfg.get("zmin", -0.2)
        _default_ittmax = 10 if mode == 0 else 99
        ittmax   = int(self.vcfg.get("ittmax",
                       self.vcfg.get("max_iter", _default_ittmax)))

        try:
            from simulflow.preparation.velestprep import Velestprep
            vp = Velestprep(output_dir=self.vel_dir)
            cmn_file = vp.make_cmn(
                ref_lat=lat_ref, ref_lon=lon_ref,
                neqs=neqs, distmax=distmax,
                isingle=mode,
                ittmax=ittmax,
                zmin=zmin, lowvelocity=0,
                vthet=vthet, stathet=stathet,
                vpvs_ratio=vpvs,
                iuseelev=0, iusestacorr=1,
                mod_file="velest.mod",
                sta_file="velest.sta",
                pha_file="velest.pha",
            )
            print(f"[VELEST] CMN written: {cmn_file}")
            return cmn_file or os.path.join(self.vel_dir, "velest.cmn")
        except ImportError:
            return self._write_cmn_fallback(lat_ref, lon_ref, neqs, distmax,
                                            mode, vpvs, vthet, stathet,
                                            zmin, ittmax)

    def _write_cmn_fallback(self, ref_lat, ref_lon, neqs, distmax,
                            isingle, vpvs, vthet, stathet, zmin, ittmax):
        """Fallback: write velest.cmn without SimulFlow."""
        invertratio = 0 if isingle == 1 else 3
        cmn_path = os.path.join(self.vel_dir, "velest.cmn")
        with open(cmn_path, "w") as f:
            f.write("velest parameters are below\n")
            f.write(f"{ref_lat:.1f}   {ref_lon * -1.0:.1f}     0            0.0      0     0.00       1\n")
            f.write(f"{neqs}      0      0.0\n")
            f.write(f"{isingle}      0\n")
            f.write(f"{distmax:.1f}   0      {zmin:5.2f}    0.20    5.00    0\n")
            f.write(f"2      0.75      {vpvs:.3f}        1\n")
            f.write(f"0.01    0.01      0.01    {vthet}     {stathet}\n")
            f.write("1       0       0        0        1\n")
            f.write("1         1         2        0\n")
            f.write("0         0         0         0         0         0         0\n")
            f.write(f"0.001   {ittmax}   {invertratio}\n")
            f.write("velest.mod\n")
            f.write("velest.sta\n")
            f.write(" \n")
            f.write("regionsnamen.dat\n")
            f.write("regionskoord.dat\n")
            f.write(" \n")
            f.write(" \n")
            f.write("velest.pha\n")
            f.write(" \n")
            f.write("main.OUT\n")
            f.write("out.CHECK\n")
            f.write("final.CNV\n")
            f.write("sta.COR\n")
        print(f"[VELEST] CMN written (fallback): {cmn_path}")
        return cmn_path

    # ── Run VELEST ────────────────────────────────────────────────────────────
    def _run_velest(self):
        exe = self._find_velest()
        if not exe:
            print("[ERROR] VELEST binary not found.")
            print("        Expected: simulflow/bin/velest/velest")
            sys.exit(1)
        print(f"[VELEST] Running: {exe}")
        try:
            from simulflow.utils.velest_run import VelestRun
            vr = VelestRun(work_dir=self.vel_dir)
            vr.run(cmn_filename="velest.cmn")
        except ImportError:
            import subprocess
            log = os.path.join(self.vel_dir, "velest.log")
            with open(log, "w") as logf:
                proc = subprocess.Popen(
                    [exe], cwd=self.vel_dir,
                    stdin=subprocess.PIPE,
                    stdout=logf, stderr=subprocess.STDOUT, text=True,
                )
                proc.stdin.write("velest.cmn\n")
                proc.stdin.flush()
                proc.communicate()
            rc = proc.returncode
            if rc != 0:
                print(f"[VELEST] Error (rc={rc}). Log: {log}")
                sys.exit(2)
            print(f"[VELEST] Finished. Log: {log}")

    # ── Public entry ──────────────────────────────────────────────────────────
    def run(self, catalog_csv: str, mode: int = 1):
        """
        catalog_csv : path to catalog_nlloc.csv (or catalog_locsat.csv etc.)
        mode        : 1 = location only (single), 0 = full inversion
        """
        print(f"[VELEST] Preparing inputs (mode={mode}: "
              f"{'locations only' if mode == 1 else 'full inversion'}) ...")
        t0 = time.time()

        # 1. Find companion picks file
        picks_csv = self._find_picks(catalog_csv)
        if not picks_csv:
            print("[VELEST] WARNING: picks CSV not found — phase file may be sparse")

        # 2. Build intermediate phase file (# header format)
        raw_pha = os.path.join(self.vel_dir, "velest_raw.pha")
        n_evs   = self._build_phase_file(catalog_csv, picks_csv, raw_pha)
        if n_evs == 0:
            print("[ERROR] No events with picks found — aborting VELEST.")
            sys.exit(1)

        # 2b. Optional quality filter (azimuthal gap / mag / min phases)
        kept = self._apply_quality_filter(raw_pha)
        if kept == 0:
            print("[ERROR] Quality filter removed all events — relax thresholds.")
            sys.exit(1)
        if kept > 0:
            n_evs = kept

        # 3. Convert to VELEST phase format using Phase2velest.convert()
        velest_pha_in = os.path.join(self.vel_dir, "velest_input.pha")
        try:
            from simulflow.preparation.phase2velest import Phase2velest
            conv = Phase2velest()
            conv.convert(raw_pha, velest_pha_in)
        except ImportError:
            # SimulFlow not available: use SeisWork's picks_to_velest_pha
            print("[VELEST] SimulFlow not found, using SeisWork converter...")
            self._convert_phase_fallback(raw_pha, velest_pha_in)

        # 4. Import phase → velest.pha (+ count neqs)
        velest_pha = os.path.join(self.vel_dir, "velest.pha")
        neqs = 0
        try:
            from simulflow.preparation.velestprep import Velestprep
            vp = Velestprep(output_dir=self.vel_dir)
            _, neqs = vp.import_pha(
                input_pha=velest_pha_in,
                output_filename="velest.pha",
            )
        except ImportError:
            shutil.copy2(velest_pha_in, velest_pha)
            neqs = self._count_velest_events(velest_pha)
        print(f"[VELEST] neqs = {neqs}")

        # 5. Station file
        sta_src = self._find_station_file()
        velest_sta = os.path.join(self.vel_dir, "velest.sta")
        if sta_src:
            self._stations2velest_sta(sta_src, velest_sta)
        else:
            print("[WARNING] No station file found — velest.sta will be missing")

        # 6. Initial velocity model
        velest_mod = os.path.join(self.vel_dir, "velest.mod")
        self._write_velest_mod(velest_mod)

        # 7. Copy static VELEST region files
        self._copy_static_files()

        # 7b. Backup initial model before VELEST possibly overwrites it (mode=0)
        velest_mod_path = os.path.join(self.vel_dir, "velest.mod")
        initial_bak    = os.path.join(self.vel_dir, "velest_initial.mod")
        if os.path.exists(velest_mod_path) and not os.path.exists(initial_bak):
            shutil.copy2(velest_mod_path, initial_bak)

        # 8. Write velest.cmn
        self._write_cmn(neqs, mode)

        # 9. Run VELEST
        self._run_velest()

        # 10. Parse final.CNV → catalog_velest.csv (pipeline continuity)
        cnv_path = os.path.join(self.vel_dir, "final.CNV")
        if os.path.exists(cnv_path):
            cat_out = os.path.join(self.vel_dir, "catalog_velest.csv")
            n_parsed = self._parse_final_cnv(cnv_path, cat_out)
            print(f"[VELEST] catalog_velest.csv: {n_parsed} events → {cat_out}")
        else:
            print("[VELEST] WARNING: final.CNV not found — VELEST may have failed")

        # 11. Report other output files
        for out_fn, out_label in [
            ("main.OUT",   "main output"),
            ("out.CHECK",  "check output"),
            ("velest.mod", "updated model"),
            ("sta.COR",    "station corrections"),
        ]:
            src = os.path.join(self.vel_dir, out_fn)
            if os.path.exists(src):
                print(f"[VELEST] Output ({out_label}): {src}")

        elapsed = time.time() - t0
        print(f"[VELEST] Done. ({elapsed:.1f}s)  Results: {self.vel_dir}")

    # ── Parse final.CNV → catalog_velest.csv ─────────────────────────────────
    def _parse_final_cnv(self, cnv_path: str, out_csv: str) -> int:
        """
        Parse VELEST final.CNV format (fixed-field, space-separated):
          16 812  941 37.63  1.1671N 127.4592E   8.09   1.70    243      0.16
          ^yy ^MMDD ^HHMM ^sec  ^lat    ^lon    ^dep  ^mag   ^gap     ^rms
          MMDD: 1-4 digits (812 → M=8,D=12 | 1012 → M=10,D=12)
          HHMM: 1-4 digits (941 → H=9,M=41 | 0941 → H=9,M=41)
        """
        from datetime import datetime as _dt
        rows = []
        with open(cnv_path) as f:
            for line in f:
                s = line.strip()
                if not s or not s[0].isdigit():
                    continue
                try:
                    rest = s.split()
                    yy_v   = int(rest[0])
                    mmdd   = rest[1].zfill(4)       # '812' → '0812', '1012' → '1012'
                    mon_v  = int(mmdd[:2])
                    day_v  = int(mmdd[2:])
                    hhmm   = rest[2].zfill(4)       # '941' → '0941'
                    hh     = int(hhmm[:2]);  mi = int(hhmm[2:])
                    sec_v  = float(rest[3])
                    lat_s  = rest[4];  lon_s = rest[5]
                    dep_v  = float(rest[6])
                    mag_s  = rest[7]
                    gap_v  = int(rest[8])
                    rms_v  = float(rest[9])

                    isec = int(sec_v);  usec = int(round((sec_v - isec) * 1e6))
                    year = 2000 + yy_v if yy_v < 100 else yy_v
                    ot   = _dt(year, mon_v, day_v, hh, mi, isec, usec)

                    lat_d = float(lat_s[:-1])
                    if lat_s[-1] == 'S':
                        lat_d *= -1
                    lon_d = float(lon_s[:-1])
                    if lon_s[-1] == 'W':
                        lon_d *= -1
                    mag_v = float(mag_s) if mag_s.replace('.','',1).replace('-','',1).isdigit() else None

                    rows.append({
                        "event_id": f"velest.{ot.strftime('%Y%m%d.%H%M%S')}",
                        "datetime": ot.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                        "lat":      round(lat_d, 4),
                        "lon":      round(lon_d, 4),
                        "depth_km": round(dep_v, 3),
                        "mag":      mag_v,
                        "rms":      rms_v,
                        "gap":      gap_v,
                        "method":   "velest",
                    })
                except (ValueError, IndexError):
                    continue

        if rows:
            pd.DataFrame(rows).to_csv(out_csv, index=False)
        return len(rows)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _convert_phase_fallback(self, raw_pha: str, out_pha: str):
        """
        Manual conversion from '# year mon...' format to VELEST phase format
        without SimulFlow (mirrors Phase2velest.convert logic).
        """
        with open(raw_pha) as fin, open(out_pha, "w") as fout:
            sec_origin = 0.0
            for line in fin:
                line = line.rstrip()
                if not line:
                    continue
                if line.startswith("#"):
                    parts = line.split()
                    year, month, day = int(parts[1]), int(parts[2]), int(parts[3])
                    hour, minute = int(parts[4]), int(parts[5])
                    sec_origin = float(parts[6])
                    lat, lon = float(parts[7]), float(parts[8])
                    depth = float(parts[9])
                    mag = float(parts[10])
                    yy = year % 100
                    lat_dir = "S" if lat < 0 else "N"
                    lon_dir = "W" if lon < 0 else "E"
                    fout.write(
                        f"\n{yy:02d}{month:02d}{day:02d} {hour:02d}{minute:02d} "
                        f"{sec_origin:5.2f} {abs(lat):07.4f}{lat_dir} "
                        f"{abs(lon):08.4f}{lon_dir} {depth:7.2f}  {mag:5.2f}\n"
                    )
                else:
                    parts = line.split()
                    if len(parts) >= 4:
                        sta   = f"{parts[0]:<6}"[:6]
                        tt    = float(parts[1])
                        phase = parts[3][0].upper()
                        abs_t = sec_origin + tt
                        fout.write(f"  {sta}  {phase}   0   {abs_t:6.2f}\n")
            fout.write("\n")
        print(f"[VELEST] Phase converted (fallback): {out_pha}")

    @staticmethod
    def parse_main_out_rms(main_out_path: str) -> list:
        """
        Parse VELEST main.OUT for RMS residual per iteration.
        Returns list of {iter, rms, datvar, mean_sqrd} dicts.
        main.OUT is FORTRAN mixed-binary — read with errors='replace'.
        Pattern:
          Iteration nr  N obtained:
          DATVAR= X.X mean sqrd residual= X.X  RMS RESIDUAL= X.X
        """
        import re
        results = []
        iter_nr = None
        pat_iter = re.compile(r'Iteration nr\s+(\d+)\s+obtained')
        pat_rms  = re.compile(
            r'DATVAR=\s*([\d.]+)\s+mean sqrd residual=\s*([\d.]+)\s+RMS RESIDUAL=\s*([\d.]+)')
        try:
            with open(main_out_path, 'rb') as fh:
                raw = fh.read()
            text = raw.decode('utf-8', errors='replace')
            for line in text.splitlines():
                m = pat_iter.search(line)
                if m:
                    iter_nr = int(m.group(1))
                    continue
                if iter_nr is not None:
                    m2 = pat_rms.search(line)
                    if m2:
                        results.append({
                            "iter"      : iter_nr,
                            "datvar"    : float(m2.group(1)),
                            "mean_sqrd" : float(m2.group(2)),
                            "rms"       : float(m2.group(3)),
                        })
                        iter_nr = None
        except Exception:
            pass
        return results

    def _count_velest_events(self, pha_file: str) -> int:
        count = 0
        try:
            with open(pha_file) as f:
                for line in f:
                    s = line.strip()
                    if s and s[0].isdigit():
                        count += 1
        except Exception:
            pass
        return max(count, 1)

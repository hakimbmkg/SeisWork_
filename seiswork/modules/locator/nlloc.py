#!/usr/bin/env python3
"""
SeisWork - NonLinLoc (NLLoc) location module
Author : HakimBMKG

Converts catalog to NLLoc PHASE format, runs NLLoc, parses .hyp output
back to SeisWork standard catalog CSV.

Reference:
  Lomax et al. (2000), Advances in Seismology with Applications, 101-134
  http://alomax.free.fr/nlloc/
"""

import os
import sys
import glob
import math
import shutil
import subprocess
import time
from datetime import datetime, timedelta

import pandas as pd


CATALOG_COLS = [
    "event_id", "datetime", "lat", "lon", "depth_km",
    "mag", "rms", "nsta", "gap", "method"
]


class NLLocLocator:
    """NonLinLoc probabilistic hypocenter location wrapper."""

    def __init__(self, cfg: dict, base_dir: str, stations: list[dict] | None = None):
        self.cfg      = cfg
        self.base_dir = base_dir
        self.lcfg     = cfg["locate"]["nlloc"]
        self.reg      = cfg["region"]
        # Optional station list ([{net,sta,lat,lon,...}]). If given and no
        # travel-time grid exists at grid_dir, run() auto-builds one sized to
        # the network spread (nlloc_grids.auto_grid_dims).
        self._stations = stations

        self.out_dir = os.path.join(base_dir, "work", "location", "nlloc")
        self.log_dir = os.path.join(base_dir, "work", "logs", "nlloc")
        self.cat_dir = os.path.join(base_dir, "work", "catalog")
        os.makedirs(self.out_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        os.makedirs(self.cat_dir, exist_ok=True)

        self.nlloc_exec  = self._find_exec("NLLoc")
        self.vel2grid    = self._find_exec("Vel2Grid")
        self.grid2time   = self._find_exec("Grid2Time")

    # catalog_dir is a public alias for cat_dir (matches other SeisWork modules,
    # e.g. GammaAssociator), so notebooks can redirect output to a per-run dir.
    @property
    def catalog_dir(self):
        return self.cat_dir

    @catalog_dir.setter
    def catalog_dir(self, value):
        self.cat_dir = str(value)
        os.makedirs(self.cat_dir, exist_ok=True)

    def _find_exec(self, name: str) -> str:
        # Prefer the bundled binary in core/bin/ (from install.sh, repo-relative
        # and portable), then fall back to PATH.
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        for c in (os.path.join(repo_root, "core", "bin", name),
                  os.path.join(self.base_dir, "core", "bin", name)):
            if os.path.exists(c):
                return c
        return shutil.which(name) or ""

    # ── Read TRANS origin from an existing travel-time grid header ────────────
    def _grid_transform_origin(self, grid_dir: str, prefix: str):
        """Return (lat, lon) parsed from a grid's TRANSFORM line.

        TRANS in the control file must match the transform used when the
        travel-time grids were generated (from their .hdr TRANSFORM line) -
        a mismatch silently shifts every hypocenter by the lat/lon offset
        between the two origins. Returns None if no grid header is found
        (grids not built yet), so the caller can fall back to region centre.
        """
        for hdr in sorted(glob.glob(os.path.join(grid_dir, f"{prefix}.P.*.time.hdr"))):
            try:
                with open(hdr) as f:
                    for line in f:
                        if "TRANSFORM" in line and "LatOrig" in line:
                            parts = line.split()
                            lat = float(parts[parts.index("LatOrig") + 1])
                            lon = float(parts[parts.index("LongOrig") + 1])
                            return lat, lon
            except (OSError, ValueError, IndexError):
                continue
        return None

    # ── Auto-generate/refresh the travel-time grid when missing ───────────────
    def _ensure_grid(self, grid_dir: str, prefix: str) -> tuple[str, str]:
        """If no travel-time grid exists at grid_dir/prefix and a station list
        was given, generate one sized to the network spread via
        nlloc_grids.auto_grid_dims (fine for a small network, coarser for a
        nationwide one). Cached by station fingerprint, same as the realtime
        path (_ensure_global_grids in _realtime_pipeline.py). Returns
        (grid_dir, prefix) unchanged if a grid already exists or no stations
        were given."""
        if glob.glob(os.path.join(grid_dir, f"{prefix}.P.*.time.hdr")):
            return grid_dir, prefix   # already built
        if not self._stations or not self.vel2grid or not self.grid2time:
            return grid_dir, prefix   # nothing to build from / build tools missing
        try:
            from seiswork.modules.locator.nlloc_grids import ensure_global_grids
            cache_root = os.path.join(self.base_dir, "work", "nlloc_global_grids")
            new_dir, new_prefix = ensure_global_grids(
                self._stations, cache_root, self.vel2grid, self.grid2time,
                log=lambda m: print(f"[NLLoc-grid] {m}", flush=True))
            if new_dir:
                return new_dir, new_prefix
        except Exception as exc:
            print(f"[NLLoc-grid] auto-generation failed: {exc} — falling back to {grid_dir}")
        return grid_dir, prefix

    # ── Write NLLoc control file ──────────────────────────────────────────────
    def _write_nlloc_ctrl(self, obs_file: str) -> str:
        grid_dir  = self.lcfg.get("grid_dir", os.path.join(self.base_dir, "config", "nlloc_grids"))
        prefix    = self.lcfg.get("model_prefix", "layer")
        grid_dir, prefix = self._ensure_grid(grid_dir, prefix)
        method    = self.lcfg.get("grid_search", "MISFIT")
        # LOCGRID save flag must be SAVE: with NO_SAVE, NLLoc still logs
        # "Finished location" but writes zero output files (no .hyp/.hdr),
        # so _parse_hyp_files() comes back empty. Confirmed with both MISFIT
        # and OCT search methods. `save_scatter` instead controls post-run
        # cleanup of the bulky per-event grid files - see _cleanup_grid_files().
        save_flag = "SAVE"
        reg       = self.reg

        origin = self._grid_transform_origin(grid_dir, prefix)
        trans_lat, trans_lon = origin if origin else (reg["lat"], reg["lon"])

        # LOCGRID search-grid: covers the configured lat/lon bbox, expressed
        # as km offsets from the TRANS origin (1 deg lat = ~111 km,
        # 1 deg lon = ~111 km * cos(lat)). Correct even when the bbox centre
        # differs from the TRANS origin (origin is fixed by the grids).
        km_per_deg_lat = 111.0
        km_per_deg_lon = 111.0 * math.cos(math.radians(trans_lat))
        x_origin  = (reg["lon_min"] - trans_lon) * km_per_deg_lon
        y_origin  = (reg["lat_min"] - trans_lat) * km_per_deg_lat
        x_span    = (reg["lon_max"] - reg["lon_min"]) * km_per_deg_lon
        y_span    = (reg["lat_max"] - reg["lat_min"]) * km_per_deg_lat
        depth_max = float(reg.get("depth_max", 60.0))
        # LOCGRID spacing: 2 km is fine for a small regional network (e.g.
        # Jailolo, ~100 km span, ~50 nodes/axis), but a nationwide region at
        # a fixed 2 km would blow up to tens of millions of nodes and make
        # NLLoc's MISFIT search hang. Cap the longer axis at MAX_NODES and
        # derive dx/dy/dz from that (same idea as nlloc_grids.auto_grid_dims).
        MAX_NODES = 300
        dx = dy = max(2.0, max(x_span, y_span) / MAX_NODES)
        dz = max(2.0, dx)   # keep depth spacing consistent with the horizontal one
        x_num = int(round(x_span / dx)) + 1
        y_num = int(round(y_span / dy)) + 1
        z_num = int(round(depth_max / dz)) + 1

        ctrl = f"""# NLLoc Control File — auto-generated by SeisWork
CONTROL 1 54321

TRANS SIMPLE {trans_lat:.4f} {trans_lon:.4f} 0.0

VGOUT {os.path.join(self.out_dir, 'model', prefix)}
VGTYPE P
VGGRID 2 101 101  0.0 -{reg['lat_max']-reg['lat_min']:.1f} 0.0  \
 {(reg['lon_max']-reg['lon_min'])/100:.3f} {(reg['lat_max']-reg['lat_min'])/100:.3f} 1.0  SLOW_LEN

LOCFILES {obs_file} NLLOC_OBS \
 {os.path.join(grid_dir, prefix)} \
 {os.path.join(self.out_dir, 'loc', 'loc')}

LOCHYPOUT SAVE_NLLOC_ALL SAVE_HYPOINV_SUM
LOCSEARCH {method} 10000 5000 0.01 8
LOCGRID {x_num} {y_num} {z_num}  {x_origin:.1f} {y_origin:.1f} 0.0   {dx:.1f} {dy:.1f} {dz:.1f}  MISFIT {save_flag}
LOCMETH EDT_OT_WT 9999.0 4 -1 -1 2 -1.0 0
LOCGAU 0.2 0.0
LOCPHASEID P P
LOCPHASEID S S
LOCQUAL2ERR 0.1 0.2 0.4 0.8 99999.9
LOCPHSTAT 9999.0 -1 9999.0 1.0 1.0 9999.9 -9999.9 9999.9
LOCANGLES ANGLES_YES 5
LOCMAG ML_HB 1.0 -0.00301 1.0
"""
        ctrl_file = os.path.join(self.out_dir, "nlloc.ctrl")
        with open(ctrl_file, "w") as f:
            f.write(ctrl)
        return ctrl_file

    # ── Convert catalog -> NLLoc OBS (NLLOC_OBS) format ───────────────────────
    def _catalog_to_obs(self, catalog_file: str) -> str:
        cat = pd.read_csv(catalog_file)
        # Associated picks are written by the associator next to its catalog
        # file (GammaAssociator.run / RealAssociator.run), not necessarily
        # under base_dir/work/catalog - notebooks often redirect catalog_dir.
        # `picks_associated.csv` is the canonical name every associator
        # writes; `picks_gamma.csv` is a fallback for older run directories.
        cat_dir = os.path.dirname(catalog_file)
        picks = pd.DataFrame()
        for _name in ("picks_associated.csv", "picks_gamma.csv"):
            _p = os.path.join(cat_dir, _name)
            if os.path.exists(_p):
                picks = pd.read_csv(_p)
                break

        obs_dir = os.path.join(self.out_dir, "obs")
        os.makedirs(obs_dir, exist_ok=True)

        from seiswork.utils.converter import catalog_picks_to_nlloc_obs
        obs_file = os.path.join(obs_dir, "all_events.obs")
        catalog_picks_to_nlloc_obs(cat, picks, obs_file)
        return obs_file

    # ── Remove bulky per-event search-grid files after parsing ───────────────
    def _cleanup_grid_files(self):
        """Delete the large 3D PDF grids (.buf/.scat/.angle) that LOCGRID
        SAVE writes per event, keeping the lightweight .hyp/.hdr summaries.
        SAVE is required for NLLoc to write .hyp at all (see
        _write_nlloc_ctrl), so this is how `save_scatter: false` keeps disk
        usage bounded without breaking the locator.
        """
        loc_dir = os.path.join(self.out_dir, "loc")
        for ext in ("*.buf", "*.scat", "*.angle"):
            for fp in glob.glob(os.path.join(loc_dir, ext)):
                try:
                    os.remove(fp)
                except OSError:
                    pass

    # ── Parse .hyp output files ───────────────────────────────────────────────
    def _parse_hyp_files(self) -> pd.DataFrame:
        loc_dir = os.path.join(self.out_dir, "loc")
        # Besides one .hyp per event, NLLoc also writes loc.sum.*.hyp (a
        # running cumulative file with every event so far) and last.hyp (a
        # copy of the most recent event). Globbing "*.hyp" would pick up all
        # three, counting each event 2-3x.
        hyp_files = [
            fp for fp in glob.glob(os.path.join(loc_dir, "*.hyp"))
            if not os.path.basename(fp).startswith(("last.", "loc.sum."))
        ]
        rows = []
        for fp in hyp_files:
            try:
                with open(fp) as f:
                    for line in f:
                        if line.startswith("HYPOCENTER"):
                            parts = line.split()
                            # HYPOCENTER  x= X  y= Y  z= Z  OT= T  ix= ix  iy= iy  iz= iz
                            d = {}
                            for i, p in enumerate(parts):
                                if p == "x=": d["x"] = float(parts[i+1])
                                if p == "y=": d["y"] = float(parts[i+1])
                                if p == "z=": d["z"] = float(parts[i+1])
                                if p == "OT=": d["ot"] = parts[i+1] + " " + parts[i+2]
                        if line.startswith("GEOGRAPHIC"):
                            parts = line.split()
                            # GEOGRAPHIC  OT yyyy mm dd hh mm ss.ss  Lat=  lon=  depth=
                            try:
                                yr,mo,dy,hr,mn = int(parts[2]),int(parts[3]),int(parts[4]),int(parts[5]),int(parts[6])
                                sec = float(parts[7])
                                lat = float(parts[9])
                                lon = float(parts[11])
                                dep = float(parts[13])
                                # NLLoc can print hh/mm >= 24/60 when the origin time
                                # crosses a day boundary without advancing the date.
                                # Normalize via timedelta into a valid datetime.
                                dt = datetime(yr, mo, dy) + timedelta(hours=hr, minutes=mn, seconds=sec)
                                rows.append({
                                    "event_id" : os.path.basename(fp).replace(".hyp",""),
                                    "datetime" : dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}",
                                    "lat"      : lat,
                                    "lon"      : lon,
                                    "depth_km" : dep,
                                    "mag"      : float("nan"),
                                    "rms"      : float("nan"),
                                    "nsta"     : 0,
                                    "gap"      : float("nan"),
                                    "method"   : "nlloc",
                                })
                            except Exception:
                                pass
                        if line.startswith("QUALITY"):
                            parts = line.split()
                            for i, p in enumerate(parts):
                                if p == "RMS" and rows:
                                    try: rows[-1]["rms"] = float(parts[i+1])
                                    except: pass
                                if p == "Nphs" and rows:
                                    try: rows[-1]["nsta"] = int(parts[i+1])
                                    except: pass
                                if p == "Gap" and rows:
                                    try: rows[-1]["gap"] = float(parts[i+1])
                                    except: pass
            except Exception:
                pass
        return pd.DataFrame(rows, columns=CATALOG_COLS)

    # ── Public entry ──────────────────────────────────────────────────────────
    def run(self, catalog_file: str):
        if not self.nlloc_exec:
            print("[ERROR] NLLoc binary not found.")
            print("        Install NonLinLoc: http://alomax.free.fr/nlloc/")
            print("        Or set path in config.")
            sys.exit(1)

        print("[NLLoc] Preparing input files ...")
        t0 = time.time()

        obs_file  = self._catalog_to_obs(catalog_file)
        ctrl_file = self._write_nlloc_ctrl(obs_file)
        os.makedirs(os.path.join(self.out_dir, "loc"), exist_ok=True)

        log = os.path.join(self.log_dir, "nlloc.log")
        print(f"[NLLoc] Running NLLoc ...")
        with open(log, "w") as logf:
            ret = subprocess.run(
                [self.nlloc_exec, ctrl_file],
                stdout=logf, stderr=subprocess.STDOUT,
                cwd=self.out_dir
            )

        if ret.returncode != 0:
            print(f"[NLLoc] Exit code {ret.returncode}. Check: {log}")

        catalog_df = self._parse_hyp_files()

        if not self.lcfg.get("save_scatter", False):
            self._cleanup_grid_files()

        if catalog_df.empty:
            print(f"[NLLoc] No .hyp files found. Check: {log}")
            return

        # Preserve event_id from the input catalog (e.g. GaMMA integer ids):
        # the .hyp id ("loc.YYYYMMDD...") breaks the link to
        # picks_associated.csv (event_index) needed by HypoDD phase.dat and
        # Result Viewer. Match by nearest origin time (tolerance 60 s).
        try:
            in_cat = pd.read_csv(catalog_file)
            if {"event_id", "datetime"} <= set(in_cat.columns) and not in_cat.empty:
                in_t  = pd.to_datetime(in_cat["datetime"], utc=True, format="mixed")
                out_t = pd.to_datetime(catalog_df["datetime"], utc=True, format="mixed")
                catalog_df["nlloc_id"] = catalog_df["event_id"]
                new_ids = []
                for k, t in enumerate(out_t):
                    dt_abs = (in_t - t).abs()
                    j = dt_abs.idxmin()
                    if pd.notna(dt_abs.loc[j]) and dt_abs.loc[j].total_seconds() <= 60.0:
                        new_ids.append(in_cat.loc[j, "event_id"])
                    else:
                        new_ids.append(catalog_df["event_id"].iloc[k])
                catalog_df["event_id"] = new_ids
                n_keep = sum(1 for a, b in zip(catalog_df["event_id"], catalog_df["nlloc_id"]) if str(a) != str(b))
                print(f"[NLLoc] input event_id preserved for {n_keep}/{len(catalog_df)} events")
        except Exception as exc:
            print(f"[NLLoc] WARNING: failed to remap input event_id: {exc}")

        out = os.path.join(self.cat_dir, "catalog_nlloc.csv")
        catalog_df.to_csv(out, index=False)
        catalog_df.to_csv(os.path.join(self.cat_dir, "catalog_located.csv"), index=False)

        elapsed = time.time() - t0
        print(f"[NLLoc] Done. {len(catalog_df)} events → {out}  ({elapsed:.1f}s)")
        return out

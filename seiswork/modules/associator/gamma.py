#!/usr/bin/env python3
"""
SeisWork — GaMMA phase association module
Author : HakimBMKG

Uses Bayesian Gaussian Mixture Model to associate picks into events.
Produces work/catalog/catalog_gamma.csv (standard SeisWork catalog format).

Reference:
  Zhu et al. (2022), JGR Solid Earth, doi:10.1029/2021JB023249
"""

import os
import sys
import time
import warnings

import numpy as np
import pandas as pd
from pyproj import Proj

warnings.filterwarnings("ignore")


# Standard SeisWork catalog columns
CATALOG_COLS = [
    "event_id", "datetime", "lat", "lon", "depth_km",
    "mag", "rms", "nsta", "gap", "method"
]


class GammaAssociator:
    """GaMMA (Bayesian GMM) phase associator."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = base_dir
        self.gcfg     = cfg["associate"]["gamma"]
        self.reg      = cfg["region"]

        self.cat_dir = os.path.join(base_dir, "work", "catalog")
        os.makedirs(self.cat_dir, exist_ok=True)

        # UTM projection centred on region
        self.proj = Proj(
            proj="utm",
            zone=int((self.reg["lon"] + 180) / 6) + 1,
            ellps="WGS84"
        )

    # catalog_dir is a public alias for cat_dir (matches SeisWork module convention)
    @property
    def catalog_dir(self):
        return self.cat_dir

    @catalog_dir.setter
    def catalog_dir(self, value):
        self.cat_dir = str(value)
        os.makedirs(self.cat_dir, exist_ok=True)

    # ── Load picks.csv → GaMMA format ─────────────────────────────────────────
    def _load_picks(self, picks_file: str) -> pd.DataFrame:
        df = pd.read_csv(picks_file)
        # Normalise column names
        df.columns = [c.lower().strip() for c in df.columns]
        rename = {
            "phase_hint" : "type",
            "phase_time" : "timestamp",
            "phase_score": "prob",
            "phase_amp"  : "amp",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df

    # ── Instrument sensitivities (counts per m/s) ─────────────────────────────
    def _load_sensitivities(self) -> dict:
        """Map NET.STA -> overall sensitivity (counts/(m/s)) from StationXML.

        SeisWork's picker measures phase_amp as peak-to-peak/2 in raw instrument
        COUNTS (see picker._measure_amp), but GaMMA's convert_picks_csv assumes
        the amplitude is ground velocity in m/s (it does log10(amp*1e2) ## cm/s).
        Feeding counts (~1e3–1e7) straight in pushes every magnitude to the +8
        ceiling. We therefore divide counts by the station sensitivity to recover
        m/s before association. Sensitivities are NOT uniform across the network
        (broadband ~3e9 vs short-period ~1e8), so a per-station map is required.
        """
        inv_file = (self.cfg.get("data", {}).get("inventory", "")
                    or self.cfg.get("magnitude", {}).get("inventory", ""))
        if not inv_file:
            return {}
        if not os.path.isabs(inv_file):
            inv_file = os.path.join(self.base_dir, inv_file)
        if not os.path.exists(inv_file):
            print(f"[GaMMA] WARNING: inventory not found ({inv_file}); "
                  "amplitudes stay in counts → magnitudes unreliable", flush=True)
            return {}
        try:
            from obspy import read_inventory
            inv = read_inventory(inv_file)
        except Exception as e:                       # noqa: BLE001
            print(f"[GaMMA] WARNING: cannot read inventory: {e}", flush=True)
            return {}
        sens = {}
        for net in inv:
            for sta in net:
                vals = [ch.response.instrument_sensitivity.value
                        for ch in sta
                        if ch.response and ch.response.instrument_sensitivity
                        and ch.response.instrument_sensitivity.value]
                if vals:
                    sens[f"{net.code}.{sta.code}"] = float(np.median(vals))
        return sens

    def _amp_counts_to_velocity(self, picks_df: pd.DataFrame) -> pd.DataFrame:
        """Convert phase_amp from raw counts to ground velocity (m/s) in place.

        Stations missing from the inventory fall back to the network-median
        sensitivity so their picks are not left grossly over-scaled. No-op when
        the inventory is unavailable (caller will warn that magnitudes are off).
        """
        if "amp" not in picks_df.columns:
            return picks_df
        sens = self._load_sensitivities()
        if not sens:
            return picks_df
        default = float(np.median(list(sens.values())))
        key = (picks_df["network"].astype(str).str.strip() + "." +
               picks_df["station"].astype(str).str.strip())
        factor = key.map(sens).fillna(default).to_numpy()
        picks_df = picks_df.copy()
        picks_df["amp"] = pd.to_numeric(picks_df["amp"], errors="coerce") / factor
        n_def = int((~key.isin(sens)).sum())
        print(f"[GaMMA] amp counts→m/s via {len(sens)} station sensitivities "
              f"({n_def} picks used network-median fallback)", flush=True)
        return picks_df

    # ── Load station list ──────────────────────────────────────────────────────
    def _load_stations(self) -> pd.DataFrame:
        sta_file = self.cfg["data"]["station_file"]
        if not os.path.isabs(sta_file):
            sta_file = os.path.join(self.base_dir, sta_file)
        if not os.path.exists(sta_file):
            print(f"[ERROR] Station file not found: {sta_file}", flush=True)
            sys.exit(1)

        # Detect number of pipe-delimited columns from first non-empty line
        ncols = 0
        with open(sta_file) as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    ncols = len(stripped.split("|"))
                    break

        try:
            if ncols >= 6:
                # 6-col format: NET|STA|LOC|LAT|LON|ELEV (SeisComp / FDSN standard)
                df = pd.read_csv(sta_file, sep="|", header=None,
                                 names=["network","station","location","lat","lon","elev"])
            elif ncols >= 5:
                # 5-col format: NET|STA|LAT|LON|ELEV
                df = pd.read_csv(sta_file, sep="|", header=None,
                                 names=["network","station","lat","lon","elev"])
            else:
                raise ValueError("unrecognized pipe-delimited format")
        except Exception:
            # Fallback: space-delimited STA LAT LON ELEV
            df = pd.read_csv(sta_file, sep=r"\s+", header=None,
                             names=["station","lat","lon","elev"])
            df.insert(0, "network", "")

        # Drop auxiliary columns (e.g. "location" from 6-col format, often empty/NaN).
        # GaMMA's convert_picks_csv() does meta.isnull().any(axis=1) to drop
        # unmatched picks — a stray all-NaN column would wipe out every row.
        df = df[[c for c in df.columns if c in ("network", "station", "lat", "lon", "elev")]]

        df["lat"]  = pd.to_numeric(df["lat"],  errors="coerce")
        df["lon"]  = pd.to_numeric(df["lon"],  errors="coerce")
        df["elev"] = pd.to_numeric(df["elev"], errors="coerce").fillna(0.0)
        df = df.dropna(subset=["lat", "lon"])
        df["id"]   = (df["network"].astype(str).str.strip() + "." +
                      df["station"].astype(str).str.strip())
        x, y = self.proj(df["lon"].values, df["lat"].values)
        df["x(km)"] = x / 1000.0
        df["y(km)"] = y / 1000.0
        df["z(km)"] = df["elev"] / 1000.0
        return df

    # ── Build GaMMA config dict ───────────────────────────────────────────────
    def _build_gamma_config(self) -> dict:
        lat1, lat2 = self.reg["lat_min"], self.reg["lat_max"]
        lon1, lon2 = self.reg["lon_min"], self.reg["lon_max"]
        x1, y1 = self.proj(lon1, lat1)
        x2, y2 = self.proj(lon2, lat2)
        x_lim = (x1 / 1000.0, x2 / 1000.0)
        y_lim = (y1 / 1000.0, y2 / 1000.0)
        z_lim = (0.0, float(self.reg.get("depth_max", 60.0)))
        # GaMMA API v0.0.1+ (from github.com/AI4EPS/GaMMA) requires these keys.
        # Old keys (xlim_km, ylim_km, zlim_km, min_picks_per_event, max_sigma) are obsolete.
        sigma = self.gcfg.get("max_sigma", 2.0)

        # DBSCAN pre-clustering keys (only read by GaMMA when use_dbscan=True;
        # without them association() raises KeyError: 'dbscan_eps').
        # eps defaults to network aperture / P velocity, so picks of the same
        # event recorded at opposite ends of the network can still cluster
        # together (a small eps tuned for dense networks leaves everything
        # unclustered/noise on a sparse, wide-aperture network like this one).
        vel_p = self.gcfg.get("vel", {}).get("p", 6.0)
        aperture_km = float(np.hypot(x_lim[1] - x_lim[0], y_lim[1] - y_lim[0]))
        dbscan_eps_default = round(aperture_km / vel_p, 1)

        return {
            "dims"             : ["x(km)", "y(km)", "z(km)"],
            "x(km)"            : x_lim,
            "y(km)"            : y_lim,
            "z(km)"            : z_lim,
            "bfgs_bounds"      : (
                (x_lim[0] - 1, x_lim[1] + 1),
                (y_lim[0] - 1, y_lim[1] + 1),
                (0.0,          z_lim[1] + 1),
                (None,         None),
            ),
            "method"           : self.gcfg.get("method", "BGMM"),
            "use_amplitude"    : self.gcfg.get("use_amplitude", True),
            "use_dbscan"       : self.gcfg.get("use_dbscan", False),
            "dbscan_eps"               : self.gcfg.get("dbscan_eps", dbscan_eps_default),
            "dbscan_min_samples"       : self.gcfg.get("dbscan_min_samples", 4),
            "dbscan_min_cluster_size"  : self.gcfg.get("dbscan_min_cluster_size", 50),
            "dbscan_max_time_space_ratio": self.gcfg.get("dbscan_max_time_space_ratio", 10),
            "oversample_factor": self.gcfg.get("oversample_factor", 5),
            # min_picks_per_eq = new API name; fall back to old key if set
            "min_picks_per_eq" : self.gcfg.get("min_picks_per_eq",
                                  self.gcfg.get("min_picks_per_event", 8)),
            # GaMMA needs 2 elements: [time_variance, amplitude_variance]
            "covariance_prior" : self.gcfg.get("covariance_prior", [5.0, 5.0]),
            "max_sigma11"      : self.gcfg.get("max_sigma11", sigma),
            "max_sigma22"      : self.gcfg.get("max_sigma22", sigma),
            "max_sigma12"      : self.gcfg.get("max_sigma12", sigma),
        }

    # ── Convert GaMMA catalog to SeisWork standard ────────────────────────────
    def _to_standard(self, events_list) -> pd.DataFrame:
        # association() returns a plain list[dict], NOT a DataFrame. Each dict
        # carries the event centre in the projected UTM coords named by `dims`
        # ("x(km)", "y(km)", "z(km)") — these must be inverse-projected back to
        # lat/lon (NOT looked up as "lat"/"lon"/"latitude", which GaMMA never sets).
        rows = []
        for ev in events_list:
            if "x(km)" in ev and "y(km)" in ev:
                lon, lat = self.proj(ev["x(km)"] * 1000.0, ev["y(km)"] * 1000.0, inverse=True)
            else:
                lon, lat = ev.get("longitude", ev.get("lon", 0)), ev.get("latitude", ev.get("lat", 0))
            mag = ev.get("magnitude", float("nan"))
            rows.append({
                "event_id" : str(ev.get("event_index", ev.get("idx", ""))),
                "datetime" : str(ev.get("time", "")),
                "lat"      : float(lat),
                "lon"      : float(lon),
                "depth_km" : float(ev.get("z(km)", ev.get("depth_km", 0))),
                "mag"      : float(mag) if mag != 999 else float("nan"),  # 999 = sentinel for "not computed"
                "rms"      : float(ev.get("sigma_time", float("nan"))),
                "nsta"     : int(ev.get("num_picks", ev.get("nsta", 0))),
                "gap"      : float("nan"),
                "method"   : "gamma",
            })
        return pd.DataFrame(rows, columns=CATALOG_COLS)

    # ── Public entry ──────────────────────────────────────────────────────────
    def run(self, picks_file: str):
        try:
            from gamma.utils import association
        except ImportError:
            print("[ERROR] GaMMA not installed. See: https://github.com/AI4EPS/GaMMA", flush=True)
            print("        pip install gamma", flush=True)
            sys.exit(1)

        print(f"[GaMMA] Loading picks: {picks_file}", flush=True)
        t0 = time.time()

        picks_df   = self._load_picks(picks_file)
        station_df = self._load_stations()
        gcfg       = self._build_gamma_config()

        # Convert phase_amp counts → m/s BEFORE association, so GaMMA's built-in
        # magnitude (calc_mag uses log10(amp*1e2) expecting m/s) is physical and
        # not pinned to the +8 ceiling. Skipped cleanly if no inventory.
        if gcfg["use_amplitude"]:
            picks_df = self._amp_counts_to_velocity(picks_df)

        # GaMMA's BGMM cannot handle NaN amplitudes (e.g. PhaseNet picks
        # without amplitude estimation). Fall back to time-only association.
        if gcfg["use_amplitude"] and ("amp" not in picks_df.columns or picks_df["amp"].isna().all()):
            print("[GaMMA] WARNING: no amplitude data in picks → use_amplitude=False (time-only)", flush=True)
            gcfg["use_amplitude"] = False
        elif gcfg["use_amplitude"] and picks_df["amp"].isna().any():
            # Partial NaN (channel missing during amplitude measurement) → drop NaN picks
            # so BGMM does not crash with "Input X contains NaN".
            n_nan = int(picks_df["amp"].isna().sum())
            picks_df = picks_df[picks_df["amp"].notna()].reset_index(drop=True)
            print(f"[GaMMA] WARNING: {n_nan} picks tanpa amplitudo dibuang (use_amplitude=True)", flush=True)

        print(f"[GaMMA] {len(picks_df)} picks, {len(station_df)} stations", flush=True)
        print(f"[GaMMA] Associating with method={gcfg['method']} (use_amplitude={gcfg['use_amplitude']}) ...", flush=True)
        print(f"[GaMMA] Region: lat=[{self.reg['lat_min']},{self.reg['lat_max']}] lon=[{self.reg['lon_min']},{self.reg['lon_max']}]", flush=True)

        # Build id as NET.STA for station matching.
        # station_id in picks.csv includes the seisbench annotation channel
        # (e.g. "7G.SP25..Pha") which does not match station df id ("7G.SP25").
        # Always prefer network + station columns when available.
        if "network" in picks_df.columns and "station" in picks_df.columns:
            picks_df["id"] = (picks_df["network"].astype(str).str.strip() + "." +
                              picks_df["station"].astype(str).str.strip())
        elif "station_id" in picks_df.columns:
            # Fallback: extract first two SEED components NET.STA
            picks_df["id"] = picks_df["station_id"].str.split(".").str[:2].str.join(".")

        # Run GaMMA
        try:
            event_idx, assignments = association(picks_df, station_df, gcfg,
                                                  method=gcfg["method"])
        except TypeError:
            event_idx, assignments = association(picks_df, station_df, gcfg)

        if len(event_idx) == 0:
            print("[GaMMA] No events associated.", flush=True)
            return

        # Merge assignments back.
        # `assignments` is a SPARSE list of (pick_idx, event_index, prob) —
        # one entry per pick that actually got assigned to an event, indexed
        # by the original picks_df row index (NOT one entry per input pick).
        # Picks that weren't associated to any event are left as event_index=-1.
        picks_df["event_index"] = -1
        picks_df["gamma_prob"]  = np.nan
        for pick_row_idx, ev_idx, prob in assignments:
            picks_df.loc[pick_row_idx, "event_index"] = ev_idx
            picks_df.loc[pick_row_idx, "gamma_prob"]  = prob
        catalog_df = self._to_standard(event_idx)

        # Save
        out_cat  = os.path.join(self.cat_dir, "catalog_gamma.csv")
        out_pick = os.path.join(self.cat_dir, "picks_gamma.csv")
        catalog_df.to_csv(out_cat,  index=False)
        picks_df.to_csv(out_pick, index=False)

        # Also write as phases.pha for VELEST
        from seiswork.utils.converter import picks_to_velest_pha
        pha_file = os.path.join(self.cat_dir, "phases.pha")
        picks_to_velest_pha(picks_df, catalog_df, pha_file)

        # Also write catalog_associated.csv / picks_associated.csv (canonical
        # names for next steps — NLLocLocator._catalog_to_obs looks these up
        # regardless of which associator produced them; see RealAssociator.run)
        catalog_df.to_csv(os.path.join(self.cat_dir, "catalog_associated.csv"), index=False)
        picks_df.to_csv(os.path.join(self.cat_dir, "picks_associated.csv"), index=False)

        elapsed = time.time() - t0
        print(f"[GaMMA] Done. {len(catalog_df)} events → {out_cat}  ({elapsed:.1f}s)", flush=True)
        return out_cat

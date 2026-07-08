#!/usr/bin/env python3
"""
SeisWork — PyOcto phase association module
Author : HakimBMKG

Runs the PyOcto associator (4D octree + EDT locate) directly in-process
(pip package, no external binary). Converts picks.csv -> PyOcto format,
associates, converts the resulting catalog back to SeisWork standard
catalog CSV.

Reference:
  Muenchmeyer, J. (2024), Seismica, doi:10.26443/seismica.v3i1.1130
"""

import os
import sys
import time

import numpy as np
import pandas as pd


CATALOG_COLS = [
    "event_id", "datetime", "lat", "lon", "depth_km",
    "mag", "rms", "nsta", "gap", "method"
]


class PyOctoAssociator:
    """PyOcto (4D octree + EDT) phase associator."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = base_dir
        self.pcfg     = cfg["associate"]["pyocto"]
        self.reg      = cfg["region"]

        self.cat_dir = os.path.join(base_dir, "work", "catalog")
        os.makedirs(self.cat_dir, exist_ok=True)

    # catalog_dir is a public alias for cat_dir (matches SeisWork module convention,
    # same as GammaAssociator.catalog_dir)
    @property
    def catalog_dir(self):
        return self.cat_dir

    @catalog_dir.setter
    def catalog_dir(self, value):
        self.cat_dir = str(value)
        os.makedirs(self.cat_dir, exist_ok=True)

    # ── Load picks.csv → PyOcto format ────────────────────────────────────────
    # PyOcto's associate() needs columns "station" (any unique site id string,
    # matched 1:1 against the station dataframe's "id"), "phase" (single-letter
    # P/S), "time" (float Unix epoch seconds, NOT a Timestamp — confirmed via
    # pyocto/src/pyocto/associator.py:544 `backend.Pick(i, row["time"], ...)`
    # which requires a float). All OTHER columns on this frame (network,
    # phase_score, phase_amp, ...) survive through associate() unchanged and
    # come back merged into `assignments` (associator.py:709-712 merges on the
    # "idx" column against the ORIGINAL picks frame) — so we keep them here
    # instead of re-joining picks_df to picks_csv after the fact.
    def _load_picks(self, picks_file: str) -> pd.DataFrame:
        df = pd.read_csv(picks_file)
        df.columns = [c.lower().strip() for c in df.columns]
        df["network"] = df["network"].astype(str).str.strip()
        df["station"] = (df["network"] + "." + df["station"].astype(str).str.strip())
        df["phase"]   = df["phase_hint"].astype(str).str.strip()
        ts = pd.to_datetime(df["phase_time"], utc=True, format="mixed")
        df["time"] = ts.apply(lambda x: x.timestamp())
        df["pick_time"] = ts
        return df

    # ── Load station list → PyOcto format ─────────────────────────────────────
    # transform_stations() needs "id" (must match picks["station"]), "latitude",
    # "longitude", "elevation" (METERS above sea level — associator.py:950-952
    # flips sign convention internally to get z in km below zero).
    def _load_stations(self) -> pd.DataFrame:
        sta_file = self.cfg["data"]["station_file"]
        if not os.path.isabs(sta_file):
            sta_file = os.path.join(self.base_dir, sta_file)
        if not os.path.exists(sta_file):
            print(f"[ERROR] Station file not found: {sta_file}", flush=True)
            sys.exit(1)
        from seiswork.utils.converter import _load_station_df
        df = _load_station_df(sta_file)
        if df.empty:
            df = pd.read_csv(sta_file, sep=r"\s+", header=None,
                              names=["station", "lat", "lon", "elev"])
            df.insert(0, "network", "")
        df["network"] = df["network"].astype(str).str.strip()
        df["station"] = df["station"].astype(str).str.strip()
        out = pd.DataFrame({
            "id"       : df["network"] + "." + df["station"],
            "latitude" : pd.to_numeric(df["lat"],  errors="coerce"),
            "longitude": pd.to_numeric(df["lon"],  errors="coerce"),
            "elevation": pd.to_numeric(df["elev"], errors="coerce").fillna(0.0),
        })
        return out.dropna(subset=["latitude", "longitude"])

    # ── Build the OctoAssociator instance ─────────────────────────────────────
    def _build_associator(self):
        import pyocto

        vel   = self.pcfg.get("velocity", {})
        vp    = float(vel.get("vp", 6.2))
        vs    = float(vel.get("vs", 3.4))
        tol   = float(self.pcfg.get("pick_match_tolerance", 1.5))
        velocity_model = pyocto.VelocityModel0D(
            p_velocity=vp, s_velocity=vs, tolerance=tol)

        lat_min, lat_max = self.reg["lat_min"], self.reg["lat_max"]
        lon_min, lon_max = self.reg["lon_min"], self.reg["lon_max"]
        depth_max = float(self.reg.get("depth_max", 60.0))

        kwargs = dict(
            n_picks             = int(self.pcfg.get("n_picks", 10)),
            n_p_picks           = int(self.pcfg.get("n_p_picks", 3)),
            n_s_picks           = int(self.pcfg.get("n_s_picks", 0)),
            n_p_and_s_picks     = int(self.pcfg.get("n_p_and_s_picks", 0)),
            min_node_size       = float(self.pcfg.get("min_node_size", 10.0)),
            min_interevent_time = float(self.pcfg.get("min_interevent_time", 3.0)),
        )
        assoc = pyocto.OctoAssociator.from_area(
            lat=(lat_min, lat_max), lon=(lon_min, lon_max),
            zlim=(0.0, depth_max),
            velocity_model=velocity_model,
            time_before=float(self.pcfg.get("time_before", 300.0)),
            **kwargs,
        )
        return assoc

    # ── Convert PyOcto catalog to SeisWork standard ───────────────────────────
    def _to_standard(self, events_df: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for _, ev in events_df.iterrows():
            t = pd.Timestamp(ev["time"], unit="s", tz="UTC")
            rows.append({
                "event_id" : f"pyocto_{int(ev['idx']):06d}",
                "datetime" : t.isoformat(),
                "lat"      : float(ev["latitude"]),
                "lon"      : float(ev["longitude"]),
                "depth_km" : float(ev["depth"]),
                "mag"      : float("nan"),
                "rms"      : float("nan"),
                "nsta"     : int(ev["picks"]),
                "gap"      : float("nan"),
                "method"   : "pyocto",
            })
        return pd.DataFrame(rows, columns=CATALOG_COLS)

    # ── Public entry ───────────────────────────────────────────────────────────
    def run(self, picks_file: str):
        try:
            import pyocto  # noqa: F401
        except ImportError:
            print("[ERROR] PyOcto not installed. See: https://github.com/yetinam/pyocto", flush=True)
            print("        pip install pyocto", flush=True)
            sys.exit(1)

        print(f"[PyOcto] Loading picks: {picks_file}", flush=True)
        t0 = time.time()

        picks_df   = self._load_picks(picks_file)
        station_df = self._load_stations()
        print(f"[PyOcto] {len(picks_df)} picks, {len(station_df)} stations", flush=True)

        assoc = self._build_associator()
        stations_t = assoc.transform_stations(station_df.copy())

        # Only pass rows whose station id is known — unmatched picks would
        # otherwise raise inside the C++ backend when looking up coordinates.
        known = set(stations_t["id"])
        picks_in = picks_df[picks_df["station"].isin(known)].reset_index(drop=True)
        n_dropped = len(picks_df) - len(picks_in)
        if n_dropped:
            print(f"[PyOcto] WARNING: {n_dropped} picks dropped (station not in station file)",
                  flush=True)

        print(f"[PyOcto] Associating (n_picks={self.pcfg.get('n_picks', 10)}, "
              f"min_node_size={self.pcfg.get('min_node_size', 10.0)} km) ...", flush=True)
        events_df, assignments_df = assoc.associate(picks_in, stations_t)

        if len(events_df) == 0:
            print("[PyOcto] No events associated.", flush=True)
            return

        events_df = assoc.transform_events(events_df)
        catalog_df = self._to_standard(events_df)

        # Build picks_pyocto.csv (superset, same convention as GammaAssociator/
        # RealAssociator: event_id/network/station/phase/pick_time/prob)
        assignments_df["event_id"] = assignments_df["event_idx"].apply(
            lambda i: f"pyocto_{int(i):06d}")
        assignments_df["network"] = assignments_df["station"].str.split(".").str[0]
        assignments_df["station"] = assignments_df["station"].str.split(".").str[1]
        picks_out = assignments_df.rename(columns={"residual": "pyocto_residual"})
        picks_out["prob"] = np.nan
        pick_cols = ["event_id", "network", "station", "phase", "pick_time",
                     "prob", "pyocto_residual"]
        picks_out = picks_out[[c for c in pick_cols if c in picks_out.columns]]

        out_cat  = os.path.join(self.cat_dir, "catalog_pyocto.csv")
        out_pick = os.path.join(self.cat_dir, "picks_pyocto.csv")
        catalog_df.to_csv(out_cat,  index=False)
        picks_out.to_csv(out_pick, index=False)

        # Canonical names — downstream converters (NLLoc, HypoDD, ...) look
        # these up regardless of which associator produced them (see
        # GammaAssociator.run / RealAssociator.run).
        catalog_df.to_csv(os.path.join(self.cat_dir, "catalog_associated.csv"), index=False)
        picks_out.to_csv(os.path.join(self.cat_dir, "picks_associated.csv"), index=False)

        elapsed = time.time() - t0
        print(f"[PyOcto] Done. {len(catalog_df)} events, {len(picks_out)} picks → {out_cat}  ({elapsed:.1f}s)",
              flush=True)
        return out_cat

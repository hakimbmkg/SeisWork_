#!/usr/bin/env python3
"""
SeisWork — Preparation: Import Catalog
Author : HakimBMKG

Imports an earthquake catalog from external sources (ISC, SeisComP/SCML)
into the standard SeisWork CSV format for use as a reference or seed catalog
in relocation workflows.

Standard output format:
  event_id, datetime, lat, lon, depth_km, mag, rms, nsta, gap, method
"""

import sys
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

CATALOG_COLS = [
    "event_id", "datetime", "lat", "lon", "depth_km",
    "mag", "rms", "nsta", "gap", "method"
]


class ImportCatalog:
    """Import an earthquake catalog from various sources into SeisWork CSV format.

    Supported sources:
      "isc"      — ISC CSV / ISC Bulletin (via ObsPy or local file)
      "seiscomp" — SCML XML (SeisComP QuakeML/SCML format)
      "quakeml"  — QuakeML XML (FDSN event service)
      "csv"      — generic CSV (with lat, lon, depth, time, mag columns)
    """

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = Path(base_dir)
        self.reg      = cfg["region"]

        self.out_dir  = self.base_dir / "work" / "catalog"
        self.out_dir.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Dispatcher
    # ─────────────────────────────────────────────────────────────────────────

    def run(self, source: str, input_path: str = None,
            output_name: str = "catalog_reference.csv") -> str:
        """Import a catalog. Returns path to the output CSV."""
        source = source.lower()
        if source == "isc":
            df = self._from_isc(input_path)
        elif source == "seiscomp":
            df = self._from_scml(input_path)
        elif source == "quakeml":
            df = self._from_quakeml(input_path)
        elif source == "csv":
            df = self._from_csv(input_path)
        else:
            print(f"[ERROR] Unknown source: {source}  "
                  f"(choose: isc / seiscomp / quakeml / csv)")
            sys.exit(1)

        df = self._filter_region(df)
        df = df[CATALOG_COLS].reset_index(drop=True)

        out = self.out_dir / output_name
        df.to_csv(out, index=False)
        print(f"[Prep] ImportCatalog ({source}) → {out}  ({len(df)} events)")
        return str(out)

    # ─────────────────────────────────────────────────────────────────────────
    # ISC Bulletin
    # ─────────────────────────────────────────────────────────────────────────

    def _from_isc(self, input_path: str = None) -> pd.DataFrame:
        """ISC Bulletin — read a local CSV file or download via ObsPy ISC client.

        ISC CSV format (from isc.ac.uk/iscbulletin/search/csv/):
          DATE,TIME,LAT,LON,SMAJ,SMIN,AZ,DEPTH,DEPERR,NDEF,NSTA,GAP,MAGTYPE,MAGNITUDE,...
        """
        if input_path and Path(input_path).exists():
            print(f"[Prep] ISC ← local file: {input_path}")
            raw = pd.read_csv(input_path, comment="#", skipinitialspace=True)
            return self._parse_isc_df(raw)

        print("[Prep] ISC ← downloading via ObsPy FDSNClient (ISC event service) ...")
        try:
            from obspy.clients.fdsn import Client
            from obspy import UTCDateTime
            client = Client("ISC")
            cat = client.get_events(
                starttime    = UTCDateTime(self.reg["starttime"]),
                endtime      = UTCDateTime(self.reg["endtime"]),
                minlatitude  = self.reg.get("lat_min"),
                maxlatitude  = self.reg.get("lat_max"),
                minlongitude = self.reg.get("lon_min"),
                maxlongitude = self.reg.get("lon_max"),
                minmagnitude = self.reg.get("mag_min", 0),
            )
        except Exception as e:
            print(f"[ERROR] ISC download failed: {e}")
            sys.exit(1)
        return self._parse_obspy_catalog(cat, method="ISC")

    def _parse_isc_df(self, raw: pd.DataFrame) -> pd.DataFrame:
        col = {c.strip().upper(): c for c in raw.columns}
        rows = []
        for i, r in raw.iterrows():
            try:
                dt = str(r.get(col.get("DATE", "DATE"), "")).strip() + "T" + \
                     str(r.get(col.get("TIME", "TIME"), "00:00:00")).strip()
                lat   = float(r.get(col.get("LAT", "LAT"), r.get("LATITUDE", 0)))
                lon   = float(r.get(col.get("LON", "LON"), r.get("LONGITUDE", 0)))
                depth = float(r.get(col.get("DEPTH", "DEPTH"), 0) or 0)
                mag   = float(r.get(col.get("MAGNITUDE", "MAGNITUDE"),
                                    r.get(col.get("MAG", "MAG"), 0)) or 0)
                nsta  = int(r.get(col.get("NSTA", "NSTA"), 0) or 0)
                gap   = float(r.get(col.get("GAP", "GAP"), 0) or 0)
                rows.append({
                    "event_id": f"ISC{i:06d}",
                    "datetime": dt, "lat": lat, "lon": lon,
                    "depth_km": depth, "mag": mag, "rms": float("nan"),
                    "nsta": nsta, "gap": gap, "method": "ISC",
                })
            except Exception:
                continue
        return pd.DataFrame(rows)

    # ─────────────────────────────────────────────────────────────────────────
    # SeisComP SCML / QuakeML
    # ─────────────────────────────────────────────────────────────────────────

    def _from_scml(self, input_path: str) -> pd.DataFrame:
        """Read SCML XML (SeisComP format) via ObsPy read_events."""
        if not input_path or not Path(input_path).exists():
            print(f"[ERROR] SCML file not found: {input_path}")
            sys.exit(1)
        print(f"[Prep] SCML ← {input_path}")
        try:
            from obspy import read_events
            cat = read_events(input_path, format="SC3ML")
        except Exception:
            try:
                from obspy import read_events
                cat = read_events(input_path)
            except Exception as e:
                print(f"[ERROR] Failed to read SCML: {e}")
                sys.exit(1)
        return self._parse_obspy_catalog(cat, method="SeisComP")

    def _from_quakeml(self, input_path: str = None) -> pd.DataFrame:
        """Read QuakeML from a local file or FDSN event service."""
        if input_path and Path(input_path).exists():
            print(f"[Prep] QuakeML ← {input_path}")
            from obspy import read_events
            cat = read_events(input_path)
        else:
            print("[Prep] QuakeML ← FDSN event service ...")
            from obspy.clients.fdsn import Client
            from obspy import UTCDateTime
            client = Client(self.cfg.get("fdsn", {}).get("client", "IRIS"))
            cat = client.get_events(
                starttime    = UTCDateTime(self.reg["starttime"]),
                endtime      = UTCDateTime(self.reg["endtime"]),
                minlatitude  = self.reg.get("lat_min"),
                maxlatitude  = self.reg.get("lat_max"),
                minlongitude = self.reg.get("lon_min"),
                maxlongitude = self.reg.get("lon_max"),
            )
        return self._parse_obspy_catalog(cat, method="QuakeML")

    def _from_csv(self, input_path: str) -> pd.DataFrame:
        """Read a generic CSV — auto-detect lat/lon/depth/time/mag columns."""
        if not input_path or not Path(input_path).exists():
            print(f"[ERROR] CSV not found: {input_path}")
            sys.exit(1)
        print(f"[Prep] CSV ← {input_path}")
        raw = pd.read_csv(input_path)
        col_map = {}
        for alias, targets in {
            "lat":      ["lat", "latitude", "LAT", "LATITUDE"],
            "lon":      ["lon", "longitude", "LON", "LONGITUDE"],
            "depth_km": ["depth_km", "depth", "DEPTH"],
            "datetime": ["datetime", "time", "origin_time", "DATE", "TIME"],
            "mag":      ["mag", "magnitude", "MAG", "MAGNITUDE"],
        }.items():
            for t in targets:
                if t in raw.columns:
                    col_map[alias] = t
                    break

        rows = []
        for i, r in raw.iterrows():
            rows.append({
                "event_id": f"CSV{i:06d}",
                "datetime": str(r.get(col_map.get("datetime", "datetime"), "")),
                "lat":      float(r.get(col_map.get("lat", "lat"), 0) or 0),
                "lon":      float(r.get(col_map.get("lon", "lon"), 0) or 0),
                "depth_km": float(r.get(col_map.get("depth_km", "depth"), 0) or 0),
                "mag":      float(r.get(col_map.get("mag", "mag"), 0) or 0),
                "rms":      float("nan"),
                "nsta":     0,
                "gap":      0.0,
                "method":   "CSV",
            })
        return pd.DataFrame(rows)

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _parse_obspy_catalog(self, cat, method: str) -> pd.DataFrame:
        rows = []
        for i, ev in enumerate(cat):
            try:
                orig = ev.preferred_origin() or ev.origins[0]
                mag_obj = ev.preferred_magnitude()
                mag   = float(mag_obj.mag) if mag_obj else float("nan")
                nsta  = orig.quality.used_station_count if orig.quality else 0
                gap   = orig.quality.azimuthal_gap      if orig.quality else 0.0
                rms   = orig.quality.standard_error      if orig.quality else float("nan")
                rid   = str(ev.resource_id).split("/")[-1]
                rows.append({
                    "event_id": rid or f"{method}{i:06d}",
                    "datetime": str(orig.time),
                    "lat":      float(orig.latitude),
                    "lon":      float(orig.longitude),
                    "depth_km": float(orig.depth / 1000),
                    "mag":      mag,
                    "rms":      float(rms) if rms else float("nan"),
                    "nsta":     int(nsta or 0),
                    "gap":      float(gap or 0.0),
                    "method":   method,
                })
            except Exception as e:
                logger.debug("failed to parse event %d: %s", i, e)
        return pd.DataFrame(rows)

    def _filter_region(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        lat_min = self.reg.get("lat_min", -90)
        lat_max = self.reg.get("lat_max",  90)
        lon_min = self.reg.get("lon_min", -180)
        lon_max = self.reg.get("lon_max",  180)
        t_start = self.reg.get("starttime", "1900-01-01")
        t_end   = self.reg.get("endtime",   "2100-01-01")
        mask = (
            (df["lat"]  >= lat_min) & (df["lat"]  <= lat_max) &
            (df["lon"]  >= lon_min) & (df["lon"]  <= lon_max) &
            (df["datetime"] >= t_start) & (df["datetime"] <= t_end)
        )
        return df[mask].copy()

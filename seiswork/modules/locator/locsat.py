#!/usr/bin/env python3
"""
SeisWork — LocSAT location module (via SeisComP screloc)
Author : HakimBMKG

Uses SeisComP's screloc (LOCSAT algorithm) as location backend.
Converts catalog+picks → SCML with origins, runs screloc offline,
parses SCML output (SeisComP schema 0.14) using ElementTree.
"""

import os
import sys
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

import pandas as pd


CATALOG_COLS = [
    "event_id", "datetime", "lat", "lon", "depth_km",
    "mag", "rms", "nsta", "gap", "method"
]

# SeisComP schema namespace used in screloc output
_SC_NS = "http://geofon.gfz.de/ns/seiscomp-schema/0.14"


class LocSATLocator:
    """LocSAT hypocenter location via SeisComP screloc."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = base_dir
        self.lcfg     = cfg["locate"]["locsat"]

        self.out_dir = os.path.join(base_dir, "work", "location", "locsat")
        self.log_dir = os.path.join(base_dir, "work", "logs", "locsat")
        os.makedirs(self.out_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)

        self.sc_bin = self._find_seiscomp()

    def _find_seiscomp(self) -> str:
        cfg_bin = self.lcfg.get("seiscomp_bin", "")
        if cfg_bin and os.path.exists(cfg_bin):
            return cfg_bin
        # User's SeisComP install — PATH first, then the SEISCOMP_ROOT env, then
        # the standard ~/seiscomp & /opt/seiscomp locations. No personal paths.
        sc_root = os.environ.get("SEISCOMP_ROOT", "")
        candidates = [
            shutil.which("seiscomp") or "",
            os.path.join(sc_root, "bin", "seiscomp") if sc_root else "",
            os.path.join(os.path.expanduser("~"), "seiscomp", "bin", "seiscomp"),
            "/opt/seiscomp/bin/seiscomp",
        ]
        for c in candidates:
            if c and os.path.exists(c):
                return c
        return ""

    def _find_screloc(self) -> str:
        if self.sc_bin:
            sc_dir = os.path.dirname(self.sc_bin)
            cand = os.path.join(sc_dir, "screloc")
            if os.path.exists(cand):
                return cand
        return shutil.which("screloc") or ""

    def _sc_db(self) -> str:
        """Database connection string for screloc (needed for inventory/LOCSAT tables)."""
        db = self.lcfg.get("database", "")
        if db:
            return db
        # Read from SeisComP global config if available
        sc_cfg = os.path.join(os.path.expanduser("~"), "seiscomp", "etc", "scmaster.cfg")
        if os.path.exists(sc_cfg):
            try:
                for line in open(sc_cfg):
                    if "dbstore.write" in line and "=" in line:
                        url = line.split("=", 1)[1].strip()
                        if url:
                            return f"mysql://{url.split('@',1)[-1]}" if "://" not in url else url
            except Exception:
                pass
        return "mysql://sysop:sysop@localhost/newseiscomp"

    # ── Write SCML picks+origins file ─────────────────────────────────────────
    def _write_scml(self, catalog_file: str) -> str:
        from seiswork.utils.converter import catalog_picks_to_scml
        scml_file = os.path.join(self.out_dir, "input.xml")
        cat = pd.read_csv(catalog_file)
        # Look for associated picks in out_dir (job dir), fall back to base_dir catalog
        picks = pd.DataFrame()
        for cand in [
            os.path.join(self.out_dir, "picks_associated.csv"),
            os.path.join(self.out_dir, "picks_gamma.csv"),
            os.path.join(self.out_dir, "picks_real.csv"),
            os.path.join(self.base_dir, "work", "catalog", "picks_gamma.csv"),
            os.path.join(self.base_dir, "work", "catalog", "picks_associated.csv"),
        ]:
            if os.path.exists(cand):
                picks = pd.read_csv(cand)
                print(f"[LocSAT] picks: {cand} ({len(picks)} rows)")
                break
        if picks.empty:
            print("[LocSAT] WARNING: no picks found — origins will use catalog coordinates only")
        catalog_picks_to_scml(cat, picks, scml_file)
        print(f"[LocSAT] SCML input: {scml_file} ({len(cat)} events)")
        return scml_file

    # ── Parse screloc SCML output (schema 0.14) → catalog DataFrame ──────────
    def _parse_scml(self, scml_file: str) -> pd.DataFrame:
        if not os.path.exists(scml_file):
            print(f"[LocSAT] output file not found: {scml_file}")
            return pd.DataFrame()
        try:
            tree = ET.parse(scml_file)
        except Exception as e:
            print(f"[LocSAT] Could not parse SCML: {e}")
            return pd.DataFrame()

        # Support both schema 0.12 (input) and 0.14 (screloc output)
        ns14 = {"sc": _SC_NS}
        ns12 = {"sc": "http://geofon.gfz-potsdam.de/ns/seiscomp3-schema/0.12"}
        origins = tree.findall(".//sc:origin", ns14)
        ns = ns14
        if not origins:
            origins = tree.findall(".//sc:origin", ns12)
            ns = ns12

        rows = []
        for o in origins:
            try:
                oid   = o.get("publicID", "")
                eid   = oid.split("/")[-1] if "/" in oid else oid
                t_str = o.findtext("sc:time/sc:value", namespaces=ns) or ""
                lat   = float(o.findtext("sc:latitude/sc:value",  namespaces=ns) or "nan")
                lon   = float(o.findtext("sc:longitude/sc:value", namespaces=ns) or "nan")
                dep_m = float(o.findtext("sc:depth/sc:value",     namespaces=ns) or "0")
                rms   = float(o.findtext("sc:quality/sc:standardError",   namespaces=ns) or "nan")
                nsta  = int(float(o.findtext("sc:quality/sc:usedStationCount", namespaces=ns) or "0"))
                gap   = float(o.findtext("sc:quality/sc:azimuthalGap",   namespaces=ns) or "nan")
                rows.append({
                    "event_id" : eid,
                    "datetime" : t_str,
                    "lat"      : lat,
                    "lon"      : lon,
                    "depth_km" : dep_m / 1000.0,
                    "mag"      : float("nan"),
                    "rms"      : rms,
                    "nsta"     : nsta,
                    "gap"      : gap,
                    "method"   : "locsat",
                })
            except Exception:
                pass
        return pd.DataFrame(rows, columns=CATALOG_COLS) if rows else pd.DataFrame()

    # ── Public entry ──────────────────────────────────────────────────────────
    def run(self, catalog_file: str):
        screloc = self._find_screloc()
        if not screloc:
            print("[ERROR] screloc not found. Install SeisComP and set seiscomp_bin in config.")
            sys.exit(1)

        print("[LocSAT] Preparing SCML input ...")
        t0 = time.time()

        scml_in  = self._write_scml(catalog_file)
        scml_out = os.path.join(self.out_dir, "located.xml")
        log      = os.path.join(self.log_dir, "screloc.log")
        # Profile name: "iasp91" (matching folder ~/seiscomp/share/locsat/tables/)
        # Not "iaspei91" — that is the old incorrect name
        profile  = self.lcfg.get("table", "iasp91")
        db       = self._sc_db()

        # screloc: re-locate existing origins using LOCSAT algorithm.
        # --replace: output only the re-located origins (not duplicates with originals)
        # Output SCML goes to stdout; stderr (screloc logs) goes to log file.
        cmd = [
            screloc,
            "--locator", "LOCSAT",
            "--profile", profile,
            "--ep", scml_in,
            "-d", db,
            "--replace",
            "-f",
        ]
        print(f"[LocSAT] Running screloc (profile={profile}) ...")
        with open(log, "w") as logf:
            ret = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=logf)

        if ret.returncode != 0:
            print(f"[LocSAT] screloc returned code {ret.returncode}. Check: {log}")

        if ret.stdout:
            with open(scml_out, "wb") as f:
                f.write(ret.stdout)
        else:
            print(f"[LocSAT] screloc produced no output. Check: {log}")

        catalog_df = self._parse_scml(scml_out)
        if catalog_df.empty:
            print("[LocSAT] No events located.")
            return

        out = os.path.join(self.out_dir, "catalog_locsat.csv")
        catalog_df.to_csv(out, index=False)
        catalog_df.to_csv(os.path.join(self.out_dir, "catalog_located.csv"), index=False)

        elapsed = time.time() - t0
        print(f"[LocSAT] Done. {len(catalog_df)} events → {out}  ({elapsed:.1f}s)")
        return out

"""
SeisWork - Focal mechanism inversion via SKHASH - by HakimBMKG

Wraps USGS SKHASH (https://code.usgs.gov/esc/SKHASH, pip-installed in env
seiswork), a Python reimplementation of HASH (Hardebeck & Shearer, 2002,
https://doi.org/10.1785/0120010200; S/P-ratio extension Hardebeck & Shearer,
2003, https://doi.org/10.1785/0120020236), to compute first-motion focal
mechanisms (strike/dip/rake) for an existing assoc+location catalog.

SKHASH itself only needs: an event catalog, P-polarity picks, a station
list, and a 1D velocity model (it ray-traces takeoff angle + azimuth
internally, compute_takeoff_azimuth=True, so no NLLoc angle output is
needed). Polarity is not produced anywhere upstream in SeisWork (PhaseNet
only gives onset time + probability), so this module derives it directly
from the raw waveform via mechanism.polarity.compute_polarities().

SKHASH's own main() calls sys.exit() at the end, so it must be invoked as a
subprocess (its installed console-script), never imported and called
in-process (that would kill the caller).
"""

import os
import shutil
import subprocess
import sys

import pandas as pd

DEFAULT_CONTROL = {
    "npolmin": 5,
    # Jailolo's 7G network is sparse/clustered (35 stations, many events sit
    # near/outside the network footprint), so azimuthal gaps of 100-300 deg
    # are common and the typical dense-network default (90) rejects everything.
    # Relaxed here; quality grading (A-F) still reflects the poor coverage.
    "max_agap": 300,
    "max_pgap": 90,
    "dang": 5,
    "nmc": 30,
    "maxout": 500,
    "badfrac": 0.1,
    "qbadfrac": 0.3,
    "delmax": 120,
    "cangle": 45,
    "prob_max": 0.1,
    "min_polarity_weight": 0.1,
    # We only ever match on station code (network/location/channel aren't
    # carried through SeisWork's pick CSVs reliably) - disable those checks.
    "require_network_match": False,
    "require_channel_match": False,
    "require_location_match": False,
}


class FocalMechanism:
    """Prepare SKHASH inputs from a SeisWork catalog+picks job, run SKHASH,
    and parse the resulting focal mechanisms."""

    def __init__(self, cfg: dict, base_dir):
        self.cfg = cfg
        self.base_dir = str(base_dir)
        self.mcfg = cfg.get("mechanism", {}).get("skhash", {})
        self.reg = cfg.get("region", {})

        self.mech_dir = os.path.join(self.base_dir, "work", "mechanism")
        os.makedirs(self.mech_dir, exist_ok=True)
        self.work_dir = os.path.join(self.mech_dir, "skhashout")
        os.makedirs(self.work_dir, exist_ok=True)

        self.skhash_bin = (self.mcfg.get("skhash_bin")
                            or shutil.which("SKHASH")
                            or os.path.join(os.path.dirname(sys.executable), "SKHASH"))

    # ── Inputs ───────────────────────────────────────────────────────────────
    def _write_vmodel(self, path):
        """1D Vp model (depth_km, vp_kms) - reuses the same Jailolo model as
        the Imaging (SIMUL2000) module (DEFAULT_GRID) unless overridden."""
        from seiswork.modules.velocity.tomography import DEFAULT_GRID
        z_nodes = self.mcfg.get("z_nodes", DEFAULT_GRID["z_nodes"])
        vp_vals = self.mcfg.get("vp_vals", DEFAULT_GRID["vp_vals"])
        rows = [(z, v) for z, v in zip(z_nodes, vp_vals) if z >= 0]
        if not rows or rows[0][0] > 0:
            # Ensure a depth=0 row exists (SKHASH model must start at surface).
            rows.insert(0, (0.0, vp_vals[0]))
        with open(path, "w") as f:
            f.write("# Depth (km), Vp (km/s)\n")
            for z, v in rows:
                f.write(f"{z:.2f}, {v:.4f}\n")

    def _write_station_file(self, path, inventory_path, network):
        from obspy import read_inventory
        inv = read_inventory(inventory_path)
        net = next((n for n in inv if n.code == network), None)
        if net is None:
            raise RuntimeError(f"Network {network} not found in {inventory_path}")
        rows = [{"station": sta.code, "latitude": sta.latitude,
                 "longitude": sta.longitude, "elevation": sta.elevation or 0.0}
                for sta in net.stations]
        pd.DataFrame(rows).to_csv(path, index=False)

    @staticmethod
    def _write_catalog(cat_df, path):
        out = cat_df.rename(columns={
            "datetime": "time", "lat": "latitude", "lon": "longitude",
            "depth_km": "depth",
        })
        for col in ("mag",):
            if col not in out.columns:
                out[col] = 0.0
        # SKHASH parses 'time' with a single fixed strptime format that
        # requires fractional seconds on every row - origins that land on an
        # exact whole second get written without ".000000" and crash the
        # whole batch (found at full-catalog scale: event index 321 had
        # "...T20:26:54" with no microseconds). Force a consistent format.
        out["time"] = pd.to_datetime(out["time"], format="mixed").dt.strftime("%Y-%m-%dT%H:%M:%S.%f")
        out = out[["time", "latitude", "longitude", "depth", "mag", "event_id"]]
        out.to_csv(path, index=False)

    @staticmethod
    def _write_polarity(pol_df, network, path):
        out = pol_df.dropna(subset=["p_polarity"]).copy()
        out["network"] = network
        out = out[["event_id", "network", "station", "p_polarity"]]
        out.to_csv(path, index=False)

    def _write_control_file(self, path, catfile, fpfile, stfile, vmodelfile,
                             outfile, outfile_polagree, outfile_polinfo,
                             outfolder_plots, params):
        lines = [
            "## SeisWork-generated SKHASH control file (by HakimBMKG)",
            "", "$catfile", catfile,
            "", "$fpfile", fpfile,
            "", "$stfile", stfile,
            "$input_format_stfile", "skhash",
            "", "$vmodel_paths", vmodelfile,
            "", "$outfile1", outfile,
            "", "$outfile_pol_agree", outfile_polagree,
            "", "$outfile_pol_info", outfile_polinfo,
        ]
        # SKHASH's control-file parser indexes value[0] unconditionally - an
        # empty $outfolder_plots value crashes it with IndexError. Omit the
        # key entirely when there's no plot folder, instead of writing blank.
        if outfolder_plots:
            lines += ["", "$outfolder_plots", outfolder_plots]
        for key, val in params.items():
            # SKHASH splits multi-value entries (e.g. $look_dep) on whitespace -
            # a Python list must render as "0 100 10", not "[0, 100, 10]".
            val_str = " ".join(str(v) for v in val) if isinstance(val, (list, tuple)) else str(val)
            lines += ["", f"${key}", val_str]
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")

    # ── Run ──────────────────────────────────────────────────────────────────
    def run(self, catalog_csv, picks_csv, sds_path, inventory_path,
            network="7G", channel=None, make_plots=True, _pol_df=None):
        """catalog_csv/picks_csv: SeisWork assoc/location job output
        (catalog_*.csv with [event_id,datetime,lat,lon,depth_km,mag,...] and
        picks_associated.csv with [event_id,network,station,phase,pick_time]).
        channel: explicit SEED channel code to force, or None (default) to
        auto-try the standard HH > BH > EH > SH priority per station.
        _pol_df: pre-computed polarity DataFrame (from polarity.py) to avoid
        re-reading waveforms when FocoNet also needs the same data.
        Returns the parsed mechanisms DataFrame (also written to OUT/out.csv).
        """
        from seiswork.modules.mechanism.polarity import compute_polarities

        cat_df = pd.read_csv(catalog_csv)

        if _pol_df is not None:
            pol_df = _pol_df
            print(f"[MECH] {len(cat_df)} events — reusing pre-computed polarities "
                  f"({pol_df['p_polarity'].notna().sum()} usable).", flush=True)
        else:
            picks_df = pd.read_csv(picks_csv)
            # Associators disagree on the phase-type column name: GaMMA writes
            # "type", REAL writes "phase".
            phase_col = "phase" if "phase" in picks_df.columns else "type"
            picks_p = picks_df[picks_df[phase_col] == "P"].copy()
            if "network" not in picks_p.columns:
                picks_p["network"] = network
            print(f"[MECH] {len(cat_df)} events, {len(picks_p)} P picks — "
                  f"computing first-motion polarities from SDS waveforms...", flush=True)
            pol_df = compute_polarities(picks_p, sds_path, channel=channel)
        n_ok = pol_df["p_polarity"].notna().sum()
        print(f"[MECH] polarity: {n_ok}/{len(pol_df)} picks usable "
              f"({pol_df['status'].value_counts().to_dict()})", flush=True)

        in_dir = os.path.join(self.work_dir, "IN")
        out_dir = os.path.join(self.work_dir, "OUT")
        os.makedirs(in_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)

        catfile = os.path.join(in_dir, "eq_catalog.csv")
        fpfile = os.path.join(in_dir, "pol.csv")
        stfile = os.path.join(in_dir, "station.csv")
        vmodelfile = os.path.join(in_dir, "vmodel.txt")
        control_path = os.path.join(self.work_dir, "control_file.txt")
        outfile = os.path.join(out_dir, "out.csv")
        outfile_polagree = os.path.join(out_dir, "out_polagree.csv")
        outfile_polinfo = os.path.join(out_dir, "out_polinfo.csv")

        self._write_catalog(cat_df, catfile)
        self._write_polarity(pol_df, network, fpfile)
        self._write_station_file(stfile, inventory_path, network)
        self._write_vmodel(vmodelfile)

        params = dict(DEFAULT_CONTROL)
        params.update(self.mcfg.get("params", {}))
        # outfolder_plots left blank intentionally: SKHASH's own beachball
        # plot (flat gray/black-white quadrants, opaque white background) is
        # replaced below by our own red/white pseudo-3D shaded-sphere render
        # (beachball_plot.regenerate_event_beachballs), built from the same
        # out.csv + out_polinfo.csv this run produces, so no need to ask
        # SKHASH to plot at all.
        self._write_control_file(
            control_path, catfile, fpfile, stfile, vmodelfile,
            outfile, outfile_polagree, outfile_polinfo, "", params)

        print(f"[MECH] running SKHASH ({self.skhash_bin}) ...", flush=True)
        proc = subprocess.run([self.skhash_bin, control_path],
                               capture_output=True, text=True)
        print(proc.stdout, flush=True)
        if proc.returncode != 0:
            print(proc.stderr, flush=True)
            raise RuntimeError(f"SKHASH exited with code {proc.returncode}")

        if not os.path.exists(outfile):
            print("[MECH] SKHASH finished but produced no out.csv "
                  "(likely 0 events met $npolmin) — see log above.", flush=True)
            return pd.DataFrame()

        result = pd.read_csv(outfile)
        print(f"[MECH] {len(result)} focal mechanism(s) computed.", flush=True)

        if make_plots:
            from seiswork.modules.mechanism.beachball_plot import regenerate_event_beachballs
            n_plots = regenerate_event_beachballs(outfile, out_dir, pol_csv=outfile_polinfo)
            print(f"[MECH] {n_plots} beachball plot(s) rendered (red/white, shaded sphere).", flush=True)

        return result

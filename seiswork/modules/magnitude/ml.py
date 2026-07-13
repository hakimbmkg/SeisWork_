#!/usr/bin/env python3
"""
SeisWork - Local Magnitude (ML) module
Author : HakimBMKG

Computes ML using ObsPy Wood-Anderson simulation + Richter (1935) formula.
Supports PAZ response from StationXML or hardcoded SM-6 / Trillium PAZ.

ML = log10(A_WA_mm) + a*log10(dist_km) + c*dist_km + b
  A_WA_mm = peak Wood-Anderson amplitude in mm.
  a,b,c = regional attenuation coefficients (Hutton & Boore 1987, BSSA 77,
  pp. 2074-2094). Verified 2026-06-22 against the published formula and
  REAL.c's built-in magnitude, both using r0=100/intercept=3.0 (not the
  r0=17/intercept=2.09 used here before, which matched no published source):
  a=1.110, c=0.00189, b=0.591, i.e. 1.110*log10(R/100) + 0.00189*(R-100) + 3.0.

Cross-checked 2026-07-04 against SeisComP's reference implementation
(libs/seiscomp/processing/magnitudes/{ML,MLv}.cpp, amplitudes/MLv.cpp).
Three deviations found and fixed:
  1. SeisWork stations are single-channel vertical (BHZ) only, which
     SeisComP treats as "MLv" and doubles the amplitude before applying the
     horizontal-calibrated ML formula (amplitudes/MLv.cpp:
     `amplitude->value *= 2.0`). Missing this made every magnitude
     ~log10(2) = 0.30 units too small. See VERTICAL_AMP_FACTOR.
  2. SeisComP's amplitude window scales with distance
     (`setSignalEnd("min(R/3 + 30, 150)")`, R in km) so the S-wave/coda stays
     inside the window at regional distance. SeisWork used a fixed +60 s
     window, shorter than S-wave travel time beyond ~150 km, so it measured
     the much smaller early P-wave instead. See _signal_window_end().
  3. SeisComP's MLv has almost no distance/depth cutoff (maxDist=8 deg,
     ~890 km; maxDepth=1000 km). SeisWork's dist_max was 300 km (hypocentral),
     too tight for a sparse/nationwide network, leaving many events with no
     magnitude. Raised default to 700 km (Hutton & Boore's validated range).
"""

import os
import sys
import time
import warnings
from multiprocessing import Pool

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")


CATALOG_COLS = [
    "event_id", "datetime", "lat", "lon", "depth_km",
    "mag", "rms", "nsta", "gap", "method"
]

# Default Wood-Anderson seismometer response
WA_PAZ = {
    "poles"      : [-6.283 + 4.7124j, -6.283 - 4.7124j],
    "zeros"      : [0 + 0j, 0 + 0j],
    "gain"       : 1.0,
    "sensitivity": 2800.0,
}

# SeisComP MLv convention (amplitudes/MLv.cpp): ML is calibrated on horizontal
# amplitude; a vertical-only measurement is doubled before it enters the ML
# formula. SeisWork networks are single-channel BHZ, so this always applies.
VERTICAL_AMP_FACTOR = 2.0


def _signal_window_end(dist_km: float) -> float:
    """Seconds after origin time at which the amplitude window closes.

    Mirrors SeisComP's default (`setSignalEnd("min(R/3 + 30, 150)")`,
    amplitudeprocessor.cpp) so the S-wave/coda, not just the smaller
    P-wave, falls inside the window at regional distance.
    """
    return min(dist_km / 3.0 + 30.0, 150.0)


def scmag_trimmed_mean(values, percent: float = 25.0):
    """Network-magnitude averaging, port of SeisComP scmag's DEFAULT method
    (Math::Statistics::computeTrimmedMean(), math/mean.cpp:130, driven by
    magtool.cpp:1301 MagTool::computeAverage): trim 25% (12.5% each tail)
    when more than 3 station magnitudes are available, else plain mean.

    Sorts the values, gives full weight to everything strictly inside the
    trimmed range, zero weight outside it, and a fractional weight to the
    boundary element on each tail (handles a trim count that isn't a whole
    number of stations). Returns the weighted mean and weighted stdev.

    :param values: station magnitude values (any order)
    :param percent: total percent trimmed across both tails (25 => 12.5%
                    off each end); 0 reduces to a plain mean.
    :return: (value, stdev). stdev is 0.0 if fewer than 2 effective values.
    """
    n = len(values)
    if n == 0:
        return None, 0.0
    if n == 1:
        return float(values[0]), 0.0

    xl = percent * 0.005   # per-tail fraction, e.g. 25% total -> 12.5% each side
    k = int(n * xl + 1e-5)

    order = sorted(range(n), key=lambda i: values[i])
    sorted_vals = [values[i] for i in order]

    weights = [0.0] * n
    for i in range(n):
        if k + 1 <= i < n - k - 1:
            weights[i] = 1.0
        elif i == k or i == n - k - 1:
            weights[i] = k + 1 - n * xl
        # else: fully trimmed tail element, weight stays 0.0

    cumw = sum(weights)
    if cumw <= 0:
        return float(np.mean(values)), 0.0
    cumv = sum(w * v for w, v in zip(weights, sorted_vals))
    value = cumv / cumw

    cumd = sum(w * (v - value) ** 2 for w, v in zip(weights, sorted_vals))
    stdev = (cumd / (cumw - 1)) ** 0.5 if cumw > 1 else 0.0
    return value, stdev


def scmag_network_magnitude(values):
    """Apply scmag's exact DEFAULT rule (magtool.cpp:1309-1315): trimmed
    mean(25%) when more than 3 station magnitudes are available, otherwise
    a plain mean. Returns (value, stdev, method_id) or (None, 0.0, "") for
    an empty input.
    """
    n = len(values)
    if n == 0:
        return None, 0.0, ""
    if n > 3:
        value, stdev = scmag_trimmed_mean(values, 25.0)
        return value, stdev, "trimmed mean(25)"
    value, stdev = scmag_trimmed_mean(values, 0.0)
    return value, stdev, "mean"


# ── Pool worker plumbing ──────────────────────────────────────────────────────
# _compute_ml_event is independent per event, so events are farmed out to a
# multiprocessing.Pool. Each worker builds its own MLMagnitude (incl. SDS
# client) and loads the StationXML inventory once via the Pool initializer,
# not once per event, since re-parsing/re-pickling a multi-MB inventory per
# task would eat the gains. Kept at module level so the initializer/task
# callables pickle cleanly under both fork and spawn.
_worker_state = {}


def _init_ml_worker(cfg, base_dir, stations, inventory_file):
    mag = MLMagnitude(cfg, base_dir)
    _worker_state["mag"]       = mag
    _worker_state["stations"]  = stations
    _worker_state["inventory"] = mag._load_inventory(inventory_file)


def _ml_worker_compute(ev: dict) -> float:
    s = _worker_state
    return s["mag"]._compute_ml_event(ev, s["stations"], s["inventory"])


class MLMagnitude:
    """Compute local magnitude ML from waveforms."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = base_dir
        self.mcfg     = cfg.get("magnitude", {})

        self.out_dir  = os.path.join(base_dir, "work", "magnitude")
        self.cat_dir  = os.path.join(base_dir, "work", "catalog")
        self.wave_dir = os.path.join(base_dir, cfg["data"]["waveform_dir"])
        os.makedirs(self.out_dir, exist_ok=True)
        os.makedirs(self.cat_dir, exist_ok=True)

        self._sds_client = self._init_sds_client(self.wave_dir)
        self._channel_cache: dict = {}   # (network, station) -> resolved SEED channel

        self.a = float(self.mcfg.get("a_coefficient",      1.110))
        self.b = float(self.mcfg.get("b_coefficient",      0.591))
        self.c = float(self.mcfg.get("linear_coefficient", 0.00189))
        self.dist_max = float(self.mcfg.get("dist_max_km", 700.0))

        # WA PAZ from config or default
        wa_cfg = self.mcfg.get("wood_anderson", {})
        self.wa_paz = {
            "poles"      : [complex(p[0], p[1]) if isinstance(p, list) else p
                            for p in wa_cfg.get("poles", WA_PAZ["poles"])],
            "zeros"      : [complex(z[0], z[1]) if isinstance(z, list) else z
                            for z in wa_cfg.get("zeros", WA_PAZ["zeros"])],
            "gain"       : float(wa_cfg.get("gain", WA_PAZ["gain"])),
            "sensitivity": float(wa_cfg.get("sensitivity", WA_PAZ["sensitivity"])),
        }

    # catalog_dir is a public alias for cat_dir (matches other SeisWork modules,
    # e.g. GammaAssociator/NLLocLocator), so notebooks can redirect output.
    @property
    def catalog_dir(self):
        return self.cat_dir

    @catalog_dir.setter
    def catalog_dir(self, value):
        self.cat_dir = str(value)
        os.makedirs(self.cat_dir, exist_ok=True)

    # ── Fast SDS lookup (avoids archive-wide glob, see _find_waveform) ───────
    @staticmethod
    def _init_sds_client(wave_dir):
        """Build an ObsPy SDS client when wave_dir follows the SDS layout
        (YEAR/NET/STA/CHAN.D/...). It constructs the exact file path per
        request (O(1)) instead of the recursive glob.glob() that
        _find_waveform falls back to, which walks the whole archive tree on
        every lookup and dominates _compute_ml_event runtime. Returns None
        for non-SDS layouts (e.g. a flat directory of .mseed files), so the
        glob fallback still works there.
        """
        try:
            year_dirs = [d for d in os.listdir(wave_dir)
                         if len(d) == 4 and d.isdigit()
                         and os.path.isdir(os.path.join(wave_dir, d))]
            if year_dirs:
                from obspy.clients.filesystem.sds import Client as SDSClient
                return SDSClient(wave_dir)
        except Exception:
            pass
        return None

    # ── Load station coordinates ──────────────────────────────────────────────
    def _load_stations(self) -> dict:
        sta_file = os.path.join(self.base_dir, self.cfg["data"]["station_file"])
        if not os.path.exists(sta_file):
            return {}

        # Detect number of pipe-delimited columns from the first non-empty line:
        # NET|STA|LOC|LAT|LON|ELEV (SeisComP/FDSN, LOC often empty -> "||") vs.
        # the older NET|STA|LAT|LON|ELEV. A fixed usecols=[0,1,2,3,4] would
        # misread the empty LOC field as "lat" and shift lat/lon by one column.
        # See GammaAssociator._load_stations for the same detection logic.
        ncols = 0
        with open(sta_file) as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    ncols = len(stripped.split("|"))
                    break

        try:
            if ncols >= 6:
                df = pd.read_csv(sta_file, sep="|", header=None,
                                 names=["network","station","location","lat","lon","elev"],
                                 usecols=[0,1,3,4,5])
            elif ncols >= 5:
                df = pd.read_csv(sta_file, sep="|", header=None,
                                 names=["network","station","lat","lon","elev"],
                                 usecols=[0,1,2,3,4])
            else:
                raise ValueError("unrecognized pipe-delimited format")
        except Exception:
            df = pd.read_csv(sta_file, sep=r"\s+", header=None,
                             names=["station","lat","lon","elev"])
        if "network" not in df.columns:
            df["network"] = ""
        return {row["station"].strip(): (str(row["network"]).strip(), row["lat"], row["lon"])
                for _, row in df.iterrows()}

    # ── Hypo-distance km ─────────────────────────────────────────────────────
    @staticmethod
    def _dist_km(lat1, lon1, lat2, lon2, depth_km):
        from obspy.geodetics import degrees2kilometers, gps2dist_azimuth
        dist_m, _, _ = gps2dist_azimuth(lat1, lon1, lat2, lon2)
        return np.sqrt((dist_m / 1000.0) ** 2 + depth_km ** 2)

    # ── Load inventory / PAZ ──────────────────────────────────────────────────
    def _load_inventory(self, inventory_file: str):
        if not inventory_file:
            print("[ML] Warning: no inventory configured — instrument response "
                  "will NOT be removed (ML will be wrong by orders of magnitude)")
            return None
        if not os.path.exists(inventory_file):
            print(f"[ML] Warning: inventory not found ({inventory_file}) — "
                  "instrument response will NOT be removed (ML will be wrong "
                  "by orders of magnitude)")
            return None
        try:
            from obspy import read_inventory
            from seiswork.utils.response_fix import fix_inventory_normalization
            inv = read_inventory(inventory_file)
            for sta, ch, old_a0, new_a0 in fix_inventory_normalization(inv):
                print(f"  [fix] {sta}.{ch}: normalization_factor in inventory "
                      f"was {old_a0} — corrected to {new_a0:.6g} (recomputed "
                      f"from poles/zeros; see response_fix.py)")
            return inv
        except Exception as e:
            print(f"[ML] Warning: could not load inventory ({e}) — using default PAZ")
            return None

    # ── Compute ML for one event ──────────────────────────────────────────────
    def _compute_ml_event(self, ev: dict, stations: dict, inventory) -> float:
        from obspy import UTCDateTime
        from obspy.signal.trigger import recursive_sta_lta

        t0 = UTCDateTime(ev["datetime"])
        lat0, lon0, dep = ev["lat"], ev["lon"], ev["depth_km"]

        ml_values = []
        for sta, (net, slat, slon) in stations.items():
            dist = self._dist_km(lat0, lon0, slat, slon, dep)
            if dist > self.dist_max or dist < 1.0:
                continue

            win_end = _signal_window_end(dist)
            channel = self._station_channel(inventory, net, sta)
            st = self._get_stream(net, sta, t0, win_end, channel)
            if st is None or not len(st):
                continue

            try:
                st.detrend("demean")
                st.taper(0.05)

                # Deconvolve the true instrument response to displacement, then
                # convolve with the Wood-Anderson PAZ - both steps are needed
                # for a genuine simulated-WA seismogram. Skipping simulate()
                # leaves raw ground displacement (~1e-7 m), ~1e4x smaller than
                # a WA trace, throwing ML off by ~-5 units.
                # Response removal is required: without it the trace stays in
                # raw counts and ML over-estimates wildly (e.g. M12). If the
                # response can't be removed (no inventory, or loc-code/epoch
                # mismatch), skip this station rather than use raw counts.
                if not inventory:
                    continue
                try:
                    st.remove_response(inventory=inventory,
                                       output="DISP",
                                       pre_filt=[0.5, 1.0, 20.0, 25.0])
                except Exception:
                    continue
                st.simulate(paz_remove=None, paz_simulate=self.wa_paz)

                # Wood-Anderson amplitude (peak-to-peak / 2) in mm. Window end
                # scales with distance (win_end, see _signal_window_end) so
                # the S-wave/coda, not just the smaller P-wave, is captured
                # at regional distance.
                for tr in st:
                    data = tr.data[max(0, int((t0 - tr.stats.starttime - 5) * tr.stats.sampling_rate)):
                                   int((t0 - tr.stats.starttime + win_end) * tr.stats.sampling_rate)]
                    if len(data) < 10:
                        continue
                    amp_m  = (np.max(data) - np.min(data)) / 2.0
                    # SeisComP MLv convention: a vertical-only amplitude is doubled
                    # before entering the horizontal-calibrated ML formula
                    # (amplitudes/MLv.cpp). All SeisWork stations are BHZ-only.
                    amp_mm = amp_m * 1000.0 * VERTICAL_AMP_FACTOR
                    if amp_mm <= 0:
                        continue
                    ml = (np.log10(amp_mm)
                          + self.a * np.log10(dist)
                          + self.c * dist
                          + self.b)
                    ml_values.append(ml)
                    break  # one component per station
            except Exception:
                pass

        # Network magnitude: scmag's default rule (trimmed mean 25% for >3
        # station magnitudes, plain mean otherwise). See scmag_network_magnitude().
        value, _stdev, _method = scmag_network_magnitude(ml_values)
        return float(value) if value is not None else float("nan")

    # ── Resolve a station's actual channel code (no band-code hardcoding) ────
    def _station_channel(self, inventory, network: str, station: str) -> str:
        """Return the SEED channel code to fetch for `station`, e.g. "BHZ".

        Looks up the real channel from the StationXML inventory (any band
        code - BH/HH/SH/EH/... - not just "HH"), preferring a vertical
        ("...Z") channel since that's what the WA/ML pipeline measures.
        Falls back to the wildcard "?H?" when the inventory has no entry
        for this station, so lookup still works for non-HH networks.
        Cached per (network, station) since the inventory is static per run.
        """
        cache = self._channel_cache
        key = (network, station)
        if key in cache:
            return cache[key]

        channel = "?H?"
        if inventory is not None:
            try:
                sub = inventory.select(network=network or "*", station=station)
                codes = sorted({ch.code for net in sub for sta in net for ch in sta})
                if codes:
                    channel = next((c for c in codes if c.endswith("Z")), codes[0])
            except Exception:
                pass

        cache[key] = channel
        return channel

    # ── Fetch waveform stream around event time (fast path + fallback) ───────
    def _get_stream(self, network: str, station: str, t0, win_end: float = 70.0,
                    channel: str = "?H?"):
        """Return a Stream covering [t0-10s, t0+win_end+10s] for `station`.

        win_end (from _signal_window_end) sizes the fetch to the same
        distance-scaled amplitude window used downstream, plus a small margin.
        channel (from _station_channel) is the station's actual SEED channel
        code when known, else the "?H?" wildcard - not hardcoded "HH?",
        which would silently miss any non-HH network (e.g. BHZ).

        Prefers the SDS client (direct path construction, see
        _init_sds_client), ~6x faster per lookup than the recursive glob
        fallback and fetches all three components in one call. Falls back
        to _find_waveform()+read() for non-SDS layouts or if the SDS lookup
        misses (e.g. archive gaps, see project_sds_waveform memory).
        """
        if self._sds_client is not None:
            try:
                st = self._sds_client.get_waveforms(network or "*", station, "*", channel,
                                                     t0 - 10, t0 + win_end + 10)
                if len(st):
                    return st
            except Exception:
                pass

        wf = self._find_waveform(station, t0)
        if wf is None:
            return None
        try:
            from obspy import read
            st = read(wf).select(station=station)
            return st if len(st) else None
        except Exception:
            return None

    # ── Find waveform file for station near event time (glob fallback) ───────
    def _find_waveform(self, station: str, t0) -> str:
        import glob
        patterns = [
            os.path.join(self.wave_dir, "**", f"*{station}*{t0.year}*{t0.julday:03d}*"),
            os.path.join(self.wave_dir, "**", f"*{station}*"),
        ]
        for pat in patterns:
            matches = glob.glob(pat, recursive=True)
            if matches:
                return matches[0]
        return None

    # ── Public entry ──────────────────────────────────────────────────────────
    def run(self, catalog_file: str, inventory_file: str = ""):
        try:
            import obspy
        except ImportError:
            print("[ERROR] ObsPy not installed: pip install obspy")
            sys.exit(1)

        print(f"[ML] Computing local magnitude ...")
        t0 = time.time()

        # Fall back to the inventory path in cfg["magnitude"]/cfg["data"] when
        # the caller doesn't pass one explicitly (see notebooks/mag_cfg for the
        # config schema). Response removal needs this inventory, otherwise ML
        # comes out ~10 units too high.
        inventory_file = (inventory_file
                          or self.mcfg.get("inventory")
                          or self.cfg.get("data", {}).get("inventory", ""))

        cat       = pd.read_csv(catalog_file)
        stations  = self._load_stations()
        inventory = self._load_inventory(inventory_file)

        if not stations:
            print("[WARNING] No station coordinates — ML computation will be skipped.")
            cat["mag"] = float("nan")
        else:
            events = [ev.to_dict() for _, ev in cat.iterrows()]

            def report(i, mags):
                if i % 50 == 0:
                    valid = [m for m in mags if not np.isnan(m)]
                    print(f"  {i}/{len(cat)}  ML range: "
                          f"{min(valid):.1f}–{max(valid):.1f}" if valid else f"  {i}/{len(cat)}")

            mags = []
            n_workers = int(self.mcfg.get("workers", min(4, os.cpu_count() or 1)))
            if n_workers > 1 and len(events) > 1:
                print(f"[ML] Computing in parallel with {n_workers} worker processes ...")
                with Pool(processes=n_workers, initializer=_init_ml_worker,
                          initargs=(self.cfg, self.base_dir, stations, inventory_file)) as pool:
                    for i, ml in enumerate(pool.imap(_ml_worker_compute, events), 1):
                        mags.append(ml)
                        report(i, mags)
            else:
                for i, ev in enumerate(events, 1):
                    mags.append(self._compute_ml_event(ev, stations, inventory))
                    report(i, mags)
            cat["mag"] = mags

        cat["method"] = cat.get("method", "nlloc") + "_ml"
        out = os.path.join(self.cat_dir, "catalog_ml.csv")
        cat.to_csv(out, index=False)

        valid = cat["mag"].dropna()
        elapsed = time.time() - t0
        if len(valid):
            print(f"[ML] Done. {len(valid)}/{len(cat)} events with ML "
                  f"({valid.min():.1f}–{valid.max():.1f}, avg={valid.mean():.2f}) → {out}  ({elapsed:.1f}s)")
        else:
            print(f"[ML] Done. No ML computed (check waveform paths). → {out}  ({elapsed:.1f}s)")
        return out

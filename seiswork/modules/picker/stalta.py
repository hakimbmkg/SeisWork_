#!/usr/bin/env python3
"""
SeisWork — STA/LTA picker module
Author : HakimBMKG

Classic recursive STA/LTA trigger using ObsPy.
Writes picks.csv to the job-specific picks_dir (or work/picks/ by default).
Supports both flat-file waveform_dir and SDS archive (cfg.data.sds_format=true).
"""

import os
import sys
import glob
import time
import warnings
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")


# ── Module-level pick functions for ProcessPoolExecutor pickling ──────────────

def _pick_stream_fn(st, pcfg: dict) -> list:
    """Run STA/LTA on a single ObsPy Stream, return list of pick dicts."""
    from obspy.signal.trigger import recursive_sta_lta, trigger_onset
    sta_s   = pcfg["sta_sec"]
    lta_s   = pcfg["lta_sec"]
    thr_on  = pcfg["thresh_on"]
    thr_off = pcfg["thresh_off"]
    fmin    = pcfg.get("freqmin",      1.0)
    fmax    = pcfg.get("freqmax",     20.0)
    min_dur = pcfg.get("min_duration", 0.5)
    picks = []
    for tr in st:
        try:
            tr = tr.copy()
            tr.detrend("demean")
            tr.filter("bandpass", freqmin=fmin, freqmax=fmax,
                      corners=4, zerophase=True)
            sr   = tr.stats.sampling_rate
            nsta = int(sta_s * sr)
            nlta = int(lta_s * sr)
            if len(tr.data) < nlta * 3:
                continue
            cft    = recursive_sta_lta(tr.data, nsta, nlta)
            onsets = trigger_onset(cft, thr_on, thr_off)
            net = tr.stats.network; sta = tr.stats.station
            loc = tr.stats.location; cha = tr.stats.channel
            for on, off in onsets:
                dur = (off - on) / sr
                if dur < min_dur:
                    continue
                t_on = tr.stats.starttime + on / sr
                seg  = tr.data[on:off]
                amp  = float((seg.max() - seg.min()) / 2.0) if len(seg) else 0.0
                picks.append({
                    "network": net, "station": sta, "location": loc,
                    "channel": cha, "station_id": f"{net}.{sta}.{loc}.{cha[:3]}",
                    "phase_hint": "P", "phase_time": t_on.isoformat(),
                    "phase_score": float(cft[on]), "phase_amp": amp,
                    "method": "stalta",
                })
        except Exception:
            pass
    return picks


def _pick_one_file(args: tuple) -> list:
    """Worker: read one waveform file and pick it (flat-file mode)."""
    fp, pcfg = args
    try:
        from obspy import read
        st = read(fp)
        return _pick_stream_fn(st, pcfg)
    except Exception:
        return []


def _pick_one_sds(args: tuple) -> list:
    """Worker: fetch one (net, sta, day) from SDS archive and pick it."""
    sds_root, net, sta, channels, t_s_ts, t_e_ts, pcfg = args
    try:
        from obspy.clients.filesystem.sds import Client as SDSClient
        from obspy import UTCDateTime
        t_s = UTCDateTime(t_s_ts); t_e = UTCDateTime(t_e_ts)
        client = SDSClient(sds_root)
        st = client.get_waveforms(net, sta, "*", channels, t_s, t_e)
        if not st or len(st) == 0:
            return []
        st.merge(fill_value="interpolate")
        return _pick_stream_fn(st, pcfg)
    except Exception:
        return []


class STALTAPicker:
    """Recursive STA/LTA phase picker (ObsPy)."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = str(base_dir)
        self.pcfg     = cfg["pick"]["stalta"]

        # ── waveform source ───────────────────────────────────────────────────
        _wv = cfg["data"]["waveform_dir"]
        self.wave_dir = _wv if os.path.isabs(_wv) else os.path.join(self.base_dir, _wv)
        self.is_sds   = cfg["data"].get("sds_format", False)

        # ── picks output dir (job-specific from GUI; fallback for CLI/notebook) ─
        _pd = cfg["data"].get("picks_dir", "")
        if _pd and os.path.isabs(_pd):
            self.picks_dir = _pd
        elif _pd:
            self.picks_dir = os.path.join(self.base_dir, _pd)
        else:
            self.picks_dir = os.path.join(self.base_dir, "work", "picks")

        self.log_dir = os.path.join(self.base_dir, "work", "logs", "stalta")
        os.makedirs(self.picks_dir, exist_ok=True)
        os.makedirs(self.log_dir,   exist_ok=True)

    # ── Flat-file collection ──────────────────────────────────────────────────
    def _collect_files(self) -> list:
        patterns = [
            os.path.join(self.wave_dir, "**", "*.mseed"),
            os.path.join(self.wave_dir, "**", "*.seed"),
            os.path.join(self.wave_dir, "**", "*.msd"),
        ]
        files = []
        for pat in patterns:
            files += glob.glob(pat, recursive=True)
        return sorted(set(files))

    # ── SDS stream generator ──────────────────────────────────────────────────
    def _iter_sds_streams(self):
        """Yield (key, Stream) from SDS archive per station-day using region range."""
        from obspy.clients.filesystem.sds import Client as SDSClient
        from obspy import UTCDateTime

        t_start  = UTCDateTime(self.cfg["region"]["starttime"])
        t_end    = UTCDateTime(self.cfg["region"]["endtime"])
        import fnmatch as _fnmatch
        network  = self.cfg["data"].get("network",  "*")
        cfg_chan  = self.cfg["data"].get("channels", "") or ""

        # ── station list: file → SDS scan fallback ────────────────────────────
        sta_file = self.cfg["data"].get("station_file", "")
        if sta_file:
            if not os.path.isabs(sta_file):
                sta_file = os.path.join(self.base_dir, sta_file)
            try:
                df = pd.read_csv(
                    sta_file, sep=r"[|\s]+", engine="python", header=None,
                    names=["network", "station", "lat", "lon", "elev"],
                    usecols=[0, 1], comment="#",
                )
                station_pairs = sorted(set(zip(
                    df["network"].astype(str).str.strip(),
                    df["station"].astype(str).str.strip(),
                )))
            except Exception:
                station_pairs = None
        else:
            station_pairs = None

        # Scan SDS directory tree: collect (net, sta) → available channel codes
        sds_root  = Path(self.wave_dir)
        req_years = set(range(t_start.year, t_end.year + 1))
        sta_channels: dict = {}
        for year_dir in sds_root.iterdir():
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            if int(year_dir.name) not in req_years:
                continue
            net_pat = network if network != "*" else "*"
            for net_dir in year_dir.glob(net_pat):
                if not net_dir.is_dir():
                    continue
                for sta_dir in net_dir.iterdir():
                    if not sta_dir.is_dir():
                        continue
                    key = (net_dir.name, sta_dir.name)
                    for chan_dir in sta_dir.iterdir():
                        if chan_dir.is_dir() and chan_dir.name.endswith(".D"):
                            sta_channels.setdefault(key, set()).add(chan_dir.name[:-2])

        if not station_pairs:
            station_pairs = sorted(sta_channels.keys())

        if not station_pairs:
            print("[STA/LTA] ERROR: no stations found in SDS or station_file.")
            return

        # Auto-detect best channel per station (same logic as phasenet.py)
        _PREF = ["HH?", "EH?", "BH?", "SH?", "EN?", "?H?", "?N?", "?Z?", "*"]

        def _best_chan(avail: set, pattern: str) -> str:
            if pattern and any(_fnmatch.fnmatch(c, pattern) for c in avail):
                return pattern
            for pat in _PREF:
                matches = [c for c in avail if _fnmatch.fnmatch(c, pat)]
                if matches:
                    return sorted(matches)[0][:2] + "?"
            return pattern or "?H?"

        sds = SDSClient(self.wave_dir)

        # Walk day by day
        day0 = UTCDateTime(t_start.year, t_start.month, t_start.day)
        t = day0
        while t < t_end:
            t_next = t + 86400
            t_s = max(t, t_start)
            t_e = min(t_next, t_end)
            for net, sta in station_pairs:
                avail = sta_channels.get((net, sta), set())
                channels = _best_chan(avail, cfg_chan)
                key = f"{net}.{sta}_{t.strftime('%Y%m%d')}"
                try:
                    st = sds.get_waveforms(net, sta, "*", channels, t_s, t_e)
                    if st and len(st) > 0:
                        st.merge(fill_value="interpolate")
                        yield key, st
                except Exception:
                    pass
            t = t_next

    # ── Flat-file stream generator ────────────────────────────────────────────
    def _iter_file_streams(self):
        from obspy import read
        for fp in self._collect_files():
            try:
                st = read(fp)
                yield fp, st
            except Exception:
                pass

    # ── Pick one merged stream ────────────────────────────────────────────────
    def _pick_stream(self, st) -> list:
        from obspy.signal.trigger import recursive_sta_lta, trigger_onset

        sta_s    = self.pcfg["sta_sec"]
        lta_s    = self.pcfg["lta_sec"]
        thr_on   = self.pcfg["thresh_on"]
        thr_off  = self.pcfg["thresh_off"]
        fmin     = self.pcfg.get("freqmin",      1.0)
        fmax     = self.pcfg.get("freqmax",     20.0)
        min_dur  = self.pcfg.get("min_duration", 0.5)

        picks = []
        for tr in st:
            try:
                tr = tr.copy()
                tr.detrend("demean")
                tr.filter("bandpass", freqmin=fmin, freqmax=fmax,
                          corners=4, zerophase=True)
                sr   = tr.stats.sampling_rate
                nsta = int(sta_s * sr)
                nlta = int(lta_s * sr)
                if len(tr.data) < nlta * 3:
                    continue
                cft    = recursive_sta_lta(tr.data, nsta, nlta)
                onsets = trigger_onset(cft, thr_on, thr_off)
                net = tr.stats.network
                sta = tr.stats.station
                loc = tr.stats.location
                cha = tr.stats.channel
                chn3 = cha[:3]
                for on, off in onsets:
                    dur = (off - on) / sr
                    if dur < min_dur:
                        continue
                    t_on  = tr.stats.starttime + on / sr
                    seg   = tr.data[on:off]
                    amp   = float((seg.max() - seg.min()) / 2.0) if len(seg) else 0.0
                    picks.append({
                        "network"    : net,
                        "station"    : sta,
                        "location"   : loc,
                        "channel"    : cha,
                        "station_id" : f"{net}.{sta}.{loc}.{chn3}",
                        "phase_hint" : "P",
                        "phase_time" : t_on.isoformat(),
                        "phase_score": float(cft[on]),
                        "phase_amp"  : amp,
                        "method"     : "stalta",
                    })
            except Exception:
                pass
        return picks

    # ── Pick one merged stream (instance method — kept for direct calls) ─────
    def _pick_stream(self, st) -> list:
        return _pick_stream_fn(st, self.pcfg)

    # ── Public entry ──────────────────────────────────────────────────────────
    def run(self):
        try:
            from obspy import read  # noqa: F401 — ensure ObsPy is present
        except ImportError:
            print("[ERROR] ObsPy not installed. Run: pip install obspy")
            sys.exit(1)

        # Each worker loads a full-day waveform (~70 MB); cap at 4 to avoid OOM.
        n_cpu     = multiprocessing.cpu_count()
        n_workers = min(int(self.pcfg.get("n_workers", 4)), n_cpu, 4)
        n_workers = max(1, n_workers)

        t0 = time.time()
        all_picks = []

        if self.is_sds:
            # Build task list from SDS generator, then run in parallel
            print(f"[STA/LTA] data_source=SDS  path={self.wave_dir}  workers={n_workers}")
            tasks = []
            for key, st in self._iter_sds_streams():
                # Extract (net, sta, t_s, t_e) from the stream for the worker
                if not st:
                    continue
                tr0 = st[0]
                # channel comes from the stream (already auto-detected upstream)
                actual_chan = tr0.stats.channel[:2] + "?"
                tasks.append((
                    self.wave_dir,
                    tr0.stats.network, tr0.stats.station,
                    actual_chan,
                    str(tr0.stats.starttime), str(tr0.stats.endtime),
                    self.pcfg,
                ))
            print(f"[STA/LTA] {len(tasks)} station-day tasks queued", flush=True)
            done = 0
            with ProcessPoolExecutor(max_workers=n_workers) as ex:
                futs = [ex.submit(_pick_one_sds, t) for t in tasks]
                for fut in as_completed(futs):
                    picks = fut.result()
                    all_picks.extend(picks)
                    done += 1
                    if done % 50 == 0:
                        print(f"  {done}/{len(tasks)} station-days  picks: {len(all_picks)}", flush=True)
        else:
            files = self._collect_files()
            if not files:
                print(f"[ERROR] No waveform files found in: {self.wave_dir}")
                sys.exit(1)
            print(f"[STA/LTA] data_source=files  {len(files)} files  workers={n_workers}")
            tasks = [(fp, self.pcfg) for fp in files]
            done  = 0
            with ProcessPoolExecutor(max_workers=n_workers) as ex:
                futs = [ex.submit(_pick_one_file, t) for t in tasks]
                for fut in as_completed(futs):
                    picks = fut.result()
                    all_picks.extend(picks)
                    done += 1
                    if done % 100 == 0:
                        print(f"  {done}/{len(files)} files  picks: {len(all_picks)}", flush=True)

        df = pd.DataFrame(all_picks)
        if df.empty:
            print("[STA/LTA] No triggers detected.")
            return None

        # picks.csv — same filename as PhaseNet/EQT for GUI jobs
        out_csv = os.path.join(self.picks_dir, "picks.csv")
        df.to_csv(out_csv, index=False)
        elapsed = time.time() - t0
        print(f"[STA/LTA] Done. {len(df)} picks → {out_csv}  ({elapsed:.1f}s)")
        return out_csv

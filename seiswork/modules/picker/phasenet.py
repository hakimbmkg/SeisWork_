#!/usr/bin/env python3
"""
SeisWork — PhaseNet picker module (seisbench backend)
Author : HakimBMKG

Uses seisbench (PyTorch) instead of TensorFlow PhaseNet.
Model weights are auto-downloaded on first use (~50 MB).

Data sources (pick.phasenet.data_source):
  "file"     - scan waveform_dir for .mseed/.seed files (default)
  "sds"      - read directly from an SDS archive
  "sds_lite" - light SDS read: scan record headers (pure-Python mseedlite),
               decode only the bytes overlapping the requested window
  "fdsn"     - download from FDSN -> save to SDS -> pick
  "sds+fdsn" - SDS first, FDSN fallback for missing station-days

  Backward compat: sds_direct: true -> data_source: sds
"""

import sys
import glob
import time
import threading
import warnings
import logging
from datetime import datetime as _datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)

_MSEED_LOCK = threading.Lock()


# ════════════════════════════════════════════════════════════════════════════
#  Multi-process SDS reader
# ════════════════════════════════════════════════════════════════════════════
#  The old single-process picker serialised MSEED reads on 1 core, leaving the
#  GPU idle. This mode runs N reader PROCESSES (each with its own libmseed, no
#  lock) that prepare annotation-ready chunks, while the main process runs the
#  GPU — I/O overlaps GPU work.
#
_MP = {}   # per-process reader globals (set by _mp_init)


def _measure_amp(raw_stream, t_pick, phase):
    """Peak-to-peak half-amplitude (counts) around the pick.
    P -> Z channel [-0.5,+2.0] s; S -> N/E channel [-0.5,+3.0] s.
    Returns nan on any failure (must not raise)."""
    import numpy as np
    from obspy import UTCDateTime
    try:
        t = UTCDateTime(t_pick)
        if phase == "P":
            t0, t1 = t - 0.5, t + 2.0
            sel = (raw_stream.select(component="Z") or
                   raw_stream.select(component="z") or
                   raw_stream)
        else:
            t0, t1 = t - 0.5, t + 3.0
            sel = (raw_stream.select(component="N") or
                   raw_stream.select(component="E") or
                   raw_stream.select(component="1") or
                   raw_stream.select(component="2") or
                   raw_stream)
        if not sel:
            return float("nan")
        tr_sl = sel[0].slice(t0, t1)
        if tr_sl is None or tr_sl.stats.npts == 0:
            return float("nan")
        data = tr_sl.data.astype(np.float64)
        return float((data.max() - data.min()) / 2.0)
    except Exception:
        return float("nan")


def _mp_init(sds_path, highpass, ann_chunk_s):
    """Initializer for each reader process — set config and suppress warnings."""
    import warnings as _w
    _w.filterwarnings("ignore")
    _MP["sds_path"]    = sds_path
    _MP["highpass"]    = float(highpass)
    _MP["ann_chunk_s"] = int(ann_chunk_s)


def _mp_read_prep(task):
    """Runs in the reader process: fetch SDS -> merge -> filter -> slice chunks.
    Returns (key, [chunk_Stream, ...]) or (key, None). Never touches torch/CUDA."""
    from obspy.clients.filesystem.sds import Client as SDSClient
    from obspy import UTCDateTime

    net, sta, loc, chan, t0, t1 = task
    t0u, t1u = UTCDateTime(t0), UTCDateTime(t1)
    key = f"sds:{net}.{sta}_{t0u.strftime('%Y%m%d')}"
    try:
        sds = SDSClient(_MP["sds_path"])
        st  = sds.get_waveforms(net, sta, loc, chan, t0u, t1u)
        if len(st) == 0:
            return key, None
        st.merge(fill_value="interpolate")
        st.detrend("demean")
        if _MP["highpass"] > 0:
            st.filter("highpass", freq=_MP["highpass"])

        cs = _MP["ann_chunk_s"]
        t     = min(tr.stats.starttime for tr in st)
        t_end = max(tr.stats.endtime   for tr in st)
        chunks = []
        while t < t_end:
            c = st.slice(t, t + cs)
            if len(c) > 0 and any(tr.stats.npts > 0 for tr in c):
                chunks.append(c)
            t = t + cs
        return key, (chunks or None)
    except Exception as e:                       # noqa: BLE001
        _emsg = str(e).lower()
        if "mmap" not in _emsg and "empty" not in _emsg:
            logger.debug("mp reader %s failed: %s", key, e)
        return key, None


def _gpu_shard_worker(cfg, base_dir, shard_tasks, shard_id):
    """Runs in a spawned worker process: pick one shard on the shared GPU and
    write picks_shard_<id>.csv. Each worker loads its own model."""
    try:
        p = PhaseNetPicker(cfg, base_dir)
        p.gpu_workers   = 1            # don't recurse into another parallel split
        p.io_processes  = 0            # serial within the worker (worker IS the parallelism)
        p._tasks_override = shard_tasks
        p._shard_id     = shard_id
        p.run()
    except Exception as e:             # noqa: BLE001
        import traceback
        print(f"[PhaseNet][shard {shard_id}] ERROR: {e}", flush=True)
        traceback.print_exc()


class PhaseNetPicker:
    """PhaseNet phase picker via seisbench + PyTorch (GPU-aware, no TF)."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = base_dir
        self.pcfg     = cfg["pick"]["phasenet"]

        self.wave_dir  = Path(base_dir) / cfg["data"]["waveform_dir"]
        # picks_dir: GUI passes a job-specific path; default work/picks/
        _pd = cfg["data"].get("picks_dir", "")
        self.picks_dir = Path(_pd) if _pd else Path(base_dir) / "work" / "picks"
        self.log_dir   = Path(base_dir) / "work" / "logs" / "phasenet"
        self.picks_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.model_name = self.pcfg.get("model",       "PhaseNet")
        self.pretrained  = self.pcfg.get("pretrained",  "stead")
        self.batch_size  = int(self.pcfg.get("batch_size",  64))
        self.highpass    = float(self.pcfg.get("highpass_hz", 1.0))
        self.p_thresh    = float(self.pcfg.get("p_threshold", 0.3))
        self.s_thresh    = float(self.pcfg.get("s_threshold", 0.3))

        # data_source — backward compat: sds_direct=true -> "sds"
        _legacy = self.pcfg.get("sds_direct", False)
        self.data_source = self.pcfg.get("data_source",
                               "sds" if _legacy else "file")

        # io_processes: 0 = legacy ThreadPool path; >0 = N reader processes
        # feeding the GPU (I/O overlaps GPU). Only for data_source "sds".
        self.io_processes = int(self.pcfg.get("io_processes", 0))

        # gpu_workers: N full worker processes on the same GPU (one model does
        # not saturate it). Task list is split into N shards; each worker writes
        # picks_shard_<id>.csv and the parent merges. 1 = off.
        self.gpu_workers = int(self.pcfg.get("gpu_workers", 1))
        # internal hooks, set when running as a shard worker:
        self._tasks_override = None   # pre-built task subset
        self._shard_id       = None   # output name picks_shard_<id>.csv

        # SDS config
        self.sds_path    = Path(self.pcfg.get("sds_path") or str(self.wave_dir))
        self.sds_chunk_s    = int(self.pcfg.get("sds_chunk_days",    1)) * 86400
        # annotate_chunk_hours: slice streams into N-hour chunks so seisbench
        # never allocates one huge tensor for a full day. Default 1 h.
        self.ann_chunk_s    = int(self.pcfg.get("annotate_chunk_hours", 1)) * 3600

        # FDSN config
        _fdsn             = cfg.get("fdsn", {})
        self.fdsn_client  = (self.pcfg.get("fdsn_client") or
                             _fdsn.get("client", "IRIS"))
        self.fdsn_user    = _fdsn.get("user",     "")
        self.fdsn_pwd     = _fdsn.get("password", "")
        self.fdsn_chunk_s = int(self.pcfg.get("fdsn_chunk_days", 1)) * 86400
        # fdsn_save_sds: only meaningful for FDSN download modes.
        _is_fdsn = self.data_source in ("fdsn", "sds+fdsn")
        self.fdsn_save_sds = bool(self.pcfg.get("fdsn_save_sds", True)) and _is_fdsn

        # DeepDenoiser pre-processing (SeisBench)
        # denoise=true → apply DeepDenoiser to each stream chunk before annotation
        self.denoise            = bool(self.pcfg.get("denoise", False))
        self.denoise_pretrained = str(self.pcfg.get("denoise_pretrained", "original"))

    # ── DeepDenoiser pre-processing ──────────────────────────────────────────
    def _denoise_chunk(self, chunk):
        """Apply DeepDenoiser to a stream chunk if denoise=true, else return as-is."""
        if not self.denoise:
            return chunk
        try:
            from seiswork.modules.picker.denoiser import denoise_stream
            return denoise_stream(chunk, pretrained=self.denoise_pretrained)
        except Exception as e:
            logger.warning(f"[DeepDenoiser] denoising failed, using original: {e}")
            return chunk

    # ── Load seisbench model ──────────────────────────────────────────────────
    def _load_model(self):
        try:
            import seisbench.models as sbm
        except ImportError:
            print("[ERROR] seisbench not installed.")
            sys.exit(1)
        import torch

        # PyTorch CPU threads → 1 to avoid BLAS conflicts with ObsPy.
        torch.set_num_threads(1)
        # cuDNN benchmark benchmarks all conv algorithms on first use —
        # can take MINUTES on H100 before any picks appear.
        torch.backends.cudnn.benchmark = False

        model_cls = getattr(sbm, self.model_name, sbm.PhaseNet)
        print(f"[PhaseNet] Loading {self.model_name} ({self.pretrained}) ...")
        try:
            model = model_cls.from_pretrained(self.pretrained)
        except Exception as e:
            # Model not cached and cannot be downloaded (no internet connection)
            print(f"[ERROR] Failed to load seisbench model '{self.pretrained}': {e}")
            print("[ERROR] Ensure the server has internet access to download the model on first run,")
            print("[ERROR] or copy the seisbench cache from another machine to ~/.seisbench/")
            sys.exit(1)
        self._on_gpu = torch.cuda.is_available()
        if self._on_gpu:
            model = model.cuda()
            torch.cuda.synchronize()
            print(f"[PhaseNet] GPU: {torch.cuda.get_device_name(0)}")
        else:
            print("[PhaseNet] CPU mode")
        model.eval()
        return model

    # =========================================================================
    # Task collection
    # =========================================================================

    def _collect_files(self) -> list:
        sds = self.cfg["data"].get("sds_format", False)
        if sds:
            patterns = [str(self.wave_dir / "*" / "*" / "*" / "*.mseed")]
        else:
            patterns = [
                str(self.wave_dir / "**" / "*.mseed"),
                str(self.wave_dir / "**" / "*.seed"),
                str(self.wave_dir / "**" / "*.msd"),
            ]
        files = []
        for pat in patterns:
            files += glob.glob(pat, recursive=True)
        return sorted(set(files))

    def _collect_sds_tasks(self) -> list:
        """Scan SDS dirs → task list per station-day within region time range."""
        from obspy import UTCDateTime
        import fnmatch
        network  = self.cfg["data"].get("network",  "*")
        cfg_chan  = self.cfg["data"].get("channels",
                       self.cfg.get("fdsn", {}).get("channels", "")) or ""
        t_start  = UTCDateTime(self.cfg["region"]["starttime"])
        t_end    = UTCDateTime(self.cfg["region"]["endtime"])
        req_years = set(range(t_start.year, t_end.year + 1))

        # stations: (net, sta) → set of channel codes found in SDS dirs
        sta_channels: dict = {}
        for year_dir in sorted(self.sds_path.iterdir()):
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            if int(year_dir.name) not in req_years:
                continue
            net_pat = network if network != "*" else "*"
            for net_dir in sorted(year_dir.glob(net_pat)):
                if not net_dir.is_dir():
                    continue
                for sta_dir in sorted(net_dir.iterdir()):
                    if not sta_dir.is_dir():
                        continue
                    key = (net_dir.name, sta_dir.name)
                    # collect channel codes from chan.D subdirectories
                    for chan_dir in sta_dir.iterdir():
                        if chan_dir.is_dir() and chan_dir.name.endswith(".D"):
                            chan_code = chan_dir.name[:-2]   # strip ".D"
                            sta_channels.setdefault(key, set()).add(chan_code)
                    if key not in sta_channels:
                        sta_channels[key] = set()

        # Restrict to config-selected stations when station_file provided
        allowed = set(self._load_station_list())
        if allowed:
            sta_channels = {k: v for k, v in sta_channels.items() if k in allowed}
            print(f"[PhaseNet] station filter: {len(sta_channels)} of selected "
                  f"{len(allowed)} station(s) found in SDS", flush=True)

        # Auto-detect channel per station when no explicit channel configured
        # or when cfg_chan matches none of the actual channels found in SDS.
        # Priority: HH? > EH? > BH? > SH? > EN? > ?H? (broadest match)
        _PREF_ORDER = ["HH?", "EH?", "BH?", "SH?", "EN?", "?H?", "?N?", "?Z?", "*"]

        def _best_chan(avail: set, pattern: str) -> str:
            """Return cfg pattern if any channel matches, else best auto-detected."""
            if pattern and any(fnmatch.fnmatch(c, pattern) for c in avail):
                return pattern
            # auto-detect: pick highest-priority 3-component set available
            for pat in _PREF_ORDER:
                matches = [c for c in avail if fnmatch.fnmatch(c, pat)]
                if len(matches) >= 3:
                    # prefer the matched band code as a wildcard (e.g. "EH?")
                    band_inst = sorted(matches)[0][:2]
                    return f"{band_inst}?"
                elif matches:
                    band_inst = sorted(matches)[0][:2]
                    return f"{band_inst}?"
            return pattern or "?H?"

        # Build tasks per station with auto-detected channel
        tasks = []
        from obspy import UTCDateTime as _UTC
        day0 = _UTC(t_start.year, t_start.month, t_start.day)
        chan_summary: dict = {}
        for (net, sta), avail in sorted(sta_channels.items()):
            chan = _best_chan(avail, cfg_chan)
            chan_summary.setdefault(chan, []).append(f"{net}.{sta}")
            t = day0
            while t < t_end:
                t_next = t + self.sds_chunk_s
                if t_next > t_start:
                    tasks.append((net, sta, "*", chan,
                                  float(max(t, t_start)),
                                  float(min(t_next, t_end))))
                t = t_next

        for chan, stas in sorted(chan_summary.items()):
            print(f"[PhaseNet] channel={chan}  stations={stas}", flush=True)

        return tasks

    def _collect_fdsn_tasks(self) -> list:
        """Task list from station_file (or SDS scan fallback) × time range."""
        from obspy import UTCDateTime
        channels = self.cfg["data"].get("channels",
                       self.cfg.get("fdsn", {}).get("channels", "HH?"))
        t_start  = UTCDateTime(self.cfg["region"]["starttime"])
        t_end    = UTCDateTime(self.cfg["region"]["endtime"])

        stations = self._load_station_list()
        if not stations:
            print("[PhaseNet] station_file missing — scanning SDS for station list ...")
            sds_tasks = self._collect_sds_tasks()
            stations  = sorted({(t[0], t[1]) for t in sds_tasks})
        if not stations:
            print("[ERROR] No station list. Set data.station_file or sds_path.")
            sys.exit(1)

        return self._build_tasks(stations, channels, t_start, t_end,
                                 self.fdsn_chunk_s)

    # =========================================================================
    # Fetch functions — return (key, raw_Stream | None), NO preprocessing
    # =========================================================================

    def _fetch_file(self, filepath: str):
        try:
            from obspy import read
            with _MSEED_LOCK:   # libmseed not thread-safe for disk reads
                st = read(filepath)
            st.merge(fill_value="interpolate")
            return filepath, st
        except Exception as e:
            logger.warning("read failed %s: %s", filepath, e)
            return filepath, None

    def _fetch_sds(self, task: tuple):
        from obspy.clients.filesystem.sds import Client as SDSClient
        from obspy import UTCDateTime
        net, sta, loc, chan, t0, t1 = task
        t0u, t1u = UTCDateTime(t0), UTCDateTime(t1)
        key = f"sds:{net}.{sta}_{t0u.strftime('%Y%m%d')}"
        try:
            with _MSEED_LOCK:   # libmseed not thread-safe for disk reads
                sds = SDSClient(str(self.sds_path))
                st  = sds.get_waveforms(net, sta, loc, chan, t0u, t1u)
            if len(st) == 0:
                return key, None
            st.merge(fill_value="interpolate")
            return key, st
        except Exception as e:
            # Empty SDS files (0-byte) are normal in sparse archives — debug only
            _emsg = str(e).lower()
            if "mmap" in _emsg or "empty" in _emsg:
                logger.debug("SDS empty file skipped %s: %s", key, e)
            else:
                logger.warning("SDS fetch failed %s: %s", key, e)
            return key, None

    def _fetch_sds_lite(self, task: tuple):
        """Light SDS fetch (see seiswork.utils.sds_lite): scan record headers,
        decode only the bytes overlapping the window. No _MSEED_LOCK needed."""
        from obspy import UTCDateTime
        net, sta, loc, chan, t0, t1 = task
        t0u, t1u = UTCDateTime(t0), UTCDateTime(t1)
        key = f"sds_lite:{net}.{sta}_{t0u.strftime('%Y%m%d')}"
        try:
            st = self._lite_reader().get_waveforms(net, sta, loc, chan, t0u, t1u)
            if len(st) == 0:
                return key, None
            st.merge(fill_value="interpolate")
            return key, st
        except Exception as e:
            _emsg = str(e).lower()
            if "mmap" in _emsg or "empty" in _emsg:
                logger.debug("SDS-lite empty file skipped %s: %s", key, e)
            else:
                logger.warning("SDS-lite fetch failed %s: %s", key, e)
            return key, None

    def _lite_reader(self):
        """Lazily build and cache the SDSLiteReader."""
        if not hasattr(self, "_lite_reader_inst"):
            from seiswork.utils.sds_lite import SDSLiteReader
            self._lite_reader_inst = SDSLiteReader(self.sds_path)
        return self._lite_reader_inst

    def _fetch_fdsn(self, task: tuple):
        from obspy import UTCDateTime
        net, sta, loc, chan, t0, t1 = task
        t0u, t1u = UTCDateTime(t0), UTCDateTime(t1)
        key = f"fdsn:{net}.{sta}_{t0u.strftime('%Y%m%d')}"
        try:
            client = self._make_fdsn_client()
            st = client.get_waveforms(net, sta, loc, chan, t0u, t1u)
            if len(st) == 0:
                return key, None
            st.merge(fill_value="interpolate")
            return key, st
        except Exception as e:
            logger.warning("FDSN fetch failed %s: %s", key, e)
            return key, None

    def _fetch_sds_fdsn(self, task: tuple):
        """SDS first; FDSN fallback if SDS returns nothing."""
        key, st = self._fetch_sds(task)
        if st is not None:
            return key, st
        from obspy import UTCDateTime
        net, sta, loc, chan, t0, t1 = task
        key = f"sds+fdsn:{net}.{sta}_{UTCDateTime(t0).strftime('%Y%m%d')}"
        _, st_fdsn = self._fetch_fdsn(task)
        return key, st_fdsn

    # =========================================================================
    # SDS writer — saves raw stream per calendar day
    # =========================================================================

    def _write_to_sds(self, st, sds_write_path: Path) -> int:
        """Write a stream to the SDS archive, split per calendar day.
        Appends to existing files. Returns the number of files written."""
        from obspy import UTCDateTime, read as obs_read

        n_written = 0
        for tr in st:
            net  = tr.stats.network
            sta  = tr.stats.station
            loc  = tr.stats.location or ""
            chan = tr.stats.channel

            # walk calendar days covered by this trace
            day = UTCDateTime(tr.stats.starttime.year,
                              tr.stats.starttime.month,
                              tr.stats.starttime.day)
            while day <= tr.stats.endtime:
                day_next = day + 86400
                tr_day   = tr.slice(day, day_next - tr.stats.delta)
                if tr_day is None or tr_day.stats.npts == 0:
                    day = day_next
                    continue

                dir_path = (sds_write_path / str(day.year) / net / sta
                            / f"{chan}.D")
                dir_path.mkdir(parents=True, exist_ok=True)
                fpath = (dir_path /
                         f"{net}.{sta}.{loc}.{chan}.D.{day.year}.{day.julday:03d}")

                try:
                    with _MSEED_LOCK:   # libmseed not thread-safe
                        if fpath.exists() and fpath.stat().st_size > 0:
                            try:
                                existing = obs_read(str(fpath), format="MSEED")
                                existing += tr_day
                                existing.merge(method=1, fill_value="interpolate")
                                existing.write(str(fpath), format="MSEED")
                            except Exception:
                                tr_day.write(str(fpath), format="MSEED")
                        else:
                            tr_day.write(str(fpath), format="MSEED")
                    n_written += 1
                except Exception as e:
                    logger.warning("SDS write failed %s: %s", fpath, e)

                day = day_next

        return n_written

    # =========================================================================
    # Preprocessing + peak extraction
    # =========================================================================

    def _merge_raw(self, st):
        """Merge only — safe to call from thread pool (no BLAS)."""
        st.merge(fill_value="interpolate")
        return st

    def _filter(self, st):
        """Detrend + highpass — must be called from main thread (uses BLAS/SciPy)."""
        st.detrend("demean")
        if self.highpass > 0:
            st.filter("highpass", freq=self.highpass)
        return st

    def _slice_chunks(self, st):
        """Yield ann_chunk_s-second sub-streams (avoids one huge tensor per day)."""
        t     = min(tr.stats.starttime for tr in st)
        t_end = max(tr.stats.endtime   for tr in st)
        while t < t_end:
            t_next = t + self.ann_chunk_s
            chunk  = st.slice(t, t_next)
            if len(chunk) > 0 and any(tr.stats.npts > 0 for tr in chunk):
                yield chunk
            t = t_next

    # Min gap (s) between same-phase picks at a station — stops S-coda
    # oscillations from producing many near-duplicate picks.
    _NMS_GAP_S = 2.0

    def _extract_picks(self, key: str, annotations,
                       raw_stream=None) -> pd.DataFrame:
        # Collect raw peaks per phase separately so NMS is applied per phase.
        raw: dict[str, list] = {"P": [], "S": []}
        try:
            for tr in annotations:
                ch = tr.stats.channel
                if "_P_" in ch or ch.endswith("_P"):
                    phase = "P";  thresh = self.p_thresh
                elif "_S_" in ch or ch.endswith("_S"):
                    phase = "S";  thresh = self.s_thresh
                else:
                    continue

                data = tr.data
                dt   = tr.stats.delta
                t0   = tr.stats.starttime
                n    = len(data)

                in_peak  = False
                peak_val = 0.0
                peak_idx = 0
                for i in range(n):
                    v = data[i]
                    if v > thresh:
                        if not in_peak:
                            in_peak = True; peak_val = v; peak_idx = i
                        elif v > peak_val:
                            peak_val = v; peak_idx = i
                    else:
                        if in_peak:
                            raw[phase].append(self._make_pick(
                                tr, phase, t0 + peak_idx * dt, peak_val,
                                raw_stream))
                            in_peak = False
                if in_peak:   # flush last peak
                    raw[phase].append(self._make_pick(
                        tr, phase, t0 + peak_idx * dt, peak_val, raw_stream))

        except Exception as e:
            logger.warning("extract failed %s: %s", key, e)

        # Non-maximum suppression: merge picks within _NMS_GAP_S of each other,
        # keeping the highest-score pick in each cluster.
        picks = []
        for phase, phase_picks in raw.items():
            if not phase_picks:
                continue
            phase_picks.sort(key=lambda p: p["phase_time"])
            cluster_best = phase_picks[0]
            for pick in phase_picks[1:]:
                t_prev = _datetime.strptime(cluster_best["phase_time"], "%Y-%m-%dT%H:%M:%S.%f")
                t_curr = _datetime.strptime(pick["phase_time"], "%Y-%m-%dT%H:%M:%S.%f")
                gap = (t_curr - t_prev).total_seconds()
                if gap < self._NMS_GAP_S:
                    # Same cluster — keep the higher-score pick
                    if pick["phase_score"] > cluster_best["phase_score"]:
                        cluster_best = pick
                else:
                    picks.append(cluster_best)
                    cluster_best = pick
            picks.append(cluster_best)

        return pd.DataFrame(picks)

    def _make_pick(self, tr, phase, t_pick, peak_val,
                   raw_stream=None) -> dict:
        raw_ch = tr.stats.channel.split("_")[0]
        chn3   = raw_ch[:3]
        net, sta, loc = tr.stats.network, tr.stats.station, tr.stats.location
        amp = (_measure_amp(raw_stream, t_pick, phase)
               if raw_stream is not None else float("nan"))
        return {
            "network"    : net,
            "station"    : sta,
            "location"   : loc,
            "channel"    : chn3 + "Z",
            "station_id" : f"{net}.{sta}.{loc}.{chn3}",
            "phase_hint" : phase,
            "phase_time" : t_pick.strftime('%Y-%m-%dT%H:%M:%S.%f'),
            "phase_score": round(float(peak_val), 4),
            "phase_amp"  : amp,
            "method"     : f"seisbench_{self.model_name.lower()}",
        }

    # =========================================================================
    # Helpers
    # =========================================================================

    def _load_station_list(self) -> list:
        sta_file = Path(self.base_dir) / self.cfg["data"].get("station_file", "")
        if not sta_file.exists():
            return []
        try:
            df = pd.read_csv(
                sta_file, sep=r"[|\s]+", engine="python", header=None,
                names=["network", "station", "lat", "lon", "elev"],
                usecols=[0, 1], comment="#",
            )
            return sorted(set(zip(
                df["network"].astype(str).str.strip(),
                df["station"].astype(str).str.strip(),
            )))
        except Exception as e:
            logger.warning("station_file read error: %s", e)
            return []

    def _make_fdsn_client(self):
        from obspy.clients.fdsn import Client as FDSNClient
        if self.fdsn_user and self.fdsn_pwd:
            return FDSNClient(self.fdsn_client,
                              user=self.fdsn_user, password=self.fdsn_pwd)
        return FDSNClient(self.fdsn_client)

    @staticmethod
    def _build_tasks(station_pairs, channels, t_start, t_end, chunk_s) -> list:
        from obspy import UTCDateTime
        day0  = UTCDateTime(t_start.year, t_start.month, t_start.day)
        tasks = []
        for net, sta in sorted(station_pairs):
            t = day0
            while t < t_end:
                t_next = t + chunk_s
                if t_next > t_start:
                    tasks.append((net, sta, "*", channels,
                                  float(max(t, t_start)),
                                  float(min(t_next, t_end))))
                t = t_next
        return tasks

    # =========================================================================
    # Parallel GPU workers — split the task list across N processes on one GPU
    # =========================================================================
    def _run_gpu_parallel(self, n_gpu: int, n_w: int):
        """Split the task list into N shards, run N worker processes on the
        same GPU (own model each), then merge picks_shard_*.csv -> picks.csv."""
        import multiprocessing as mp

        ds = self.data_source
        if ds in ("sds", "sds_lite"):
            tasks = self._collect_sds_tasks()
        elif ds in ("fdsn", "sds+fdsn"):
            tasks = self._collect_fdsn_tasks()
        else:
            tasks = self._collect_files()
        if not tasks:
            print(f"[ERROR] No input found (data_source={ds})")
            sys.exit(1)

        n_gpu = max(1, min(n_gpu, len(tasks)))
        # Round-robin split -> balanced load; deterministic, so the same n_gpu
        # always reproduces the same shard i.
        shards = [tasks[i::n_gpu] for i in range(n_gpu)]

        # ── RESUME: skip shards already completed by a previous run ──────────
        # picks_dir is job-specific, so an existing picks_shard_<i>.csv came
        # from an earlier partial run of THIS job — only re-pick the missing
        # shards. A successful merge deletes the shard files.
        def _shard_done(i):
            f = self.picks_dir / f"picks_shard_{i}.csv"
            return f.exists() and f.stat().st_size > 0
        todo = [i for i in range(n_gpu) if not _shard_done(i)]
        done = [i for i in range(n_gpu) if _shard_done(i)]
        if done:
            print(f"[PhaseNet] RESUME: shard(s) {done} already complete "
                  f"→ re-picking only {todo}", flush=True)

        print(f"[PhaseNet] GPU-parallel: {len(tasks)} tasks (data_source={ds}) "
              f"→ {n_gpu} shard(s), running {len(todo)} worker(s) on the GPU", flush=True)
        t0 = time.time()
        ctx = mp.get_context("spawn")
        procs = {}
        for i in todo:
            pr = ctx.Process(target=_gpu_shard_worker,
                             args=(self.cfg, self.base_dir, shards[i], i), daemon=False)
            pr.start()
            procs[i] = pr
            print(f"[PhaseNet]   worker {i}: {len(shards[i])} tasks (pid {pr.pid})", flush=True)
        for pr in procs.values():
            pr.join()

        # ── detect workers that produced NO shard output ─────────────────────
        # Every worker must write picks_shard_<id>.csv; merging without one
        # silently drops its station-days. Trust the artifact, not the exit
        # code (an OOM kill gives -9, a caught exception still exits 0).
        failed = []
        for i, pr in procs.items():
            shard_csv = self.picks_dir / f"picks_shard_{i}.csv"
            if not shard_csv.exists():
                failed.append((i, pr.exitcode, len(shards[i])))
        if failed:
            lost = sum(n for _, _, n in failed)
            bar = "=" * 60
            print(f"[PhaseNet] {bar}", flush=True)
            print(f"[PhaseNet] FATAL: {len(failed)}/{n_gpu} GPU worker(s) died without "
                  f"output → {lost} task(s) UNPICKED:", flush=True)
            for i, ec, n in failed:
                if ec == -9:
                    reason = "OOM-killed (SIGKILL)"
                elif ec == 0:
                    reason = "exited 0 but wrote no shard (worker error — see log)"
                else:
                    reason = f"exitcode={ec}"
                print(f"[PhaseNet]   worker {i}: {reason} — {n} station-day(s) LOST", flush=True)
            print(f"[PhaseNet] Refusing to merge a PARTIAL catalog. Just re-run "
                  f"this same job — RESUME re-picks ONLY the missing shard(s) as "
                  f"fewer worker(s) (no RAM contention; ~6 GB/worker).", flush=True)
            print(f"[PhaseNet] {bar}", flush=True)
            raise RuntimeError(f"{len(failed)} GPU worker(s) died — {lost} tasks unpicked "
                               f"(likely OOM; reduce gpu_workers)")

        # ── merge shard picks ────────────────────────────────────────────────
        shard_files = sorted(self.picks_dir.glob("picks_shard_*.csv"))
        frames = []
        for f in shard_files:
            try:
                d = pd.read_csv(f)
                if not d.empty:
                    frames.append(d)
            except Exception:
                pass
        if not frames:
            print("[PhaseNet] No picks produced by any worker.")
            return None
        result = pd.concat(frames, ignore_index=True)
        result.sort_values("phase_time", inplace=True)
        result.reset_index(drop=True, inplace=True)
        out = self.picks_dir / "picks.csv"
        result.to_csv(out, index=False)
        for f in shard_files:          # tidy up the shard files
            try: f.unlink()
            except Exception: pass

        elapsed = time.time() - t0
        p_count = len(result[result["phase_hint"] == "P"])
        s_count = len(result[result["phase_hint"] == "S"])
        print(f"[PhaseNet] Done (GPU×{n_gpu}). {len(result)} picks "
              f"(P={p_count}, S={s_count}) → {out}  ({elapsed:.1f}s total)")
        return str(out)

    # =========================================================================
    # multi-process SDS reader feeding the GPU
    # =========================================================================
    def _run_mp(self, n_proc: int):
        """N reader processes (fetch+merge+filter+slice) feed the GPU in the
        main process. Pool uses 'spawn' (no inherited CUDA); readers never
        import torch. imap_unordered overlaps I/O with GPU work."""
        import multiprocessing as mp
        import torch

        tasks = self._collect_sds_tasks()
        if not tasks:
            print(f"[ERROR] No SDS input found (path={self.sds_path})")
            sys.exit(1)

        print(f"[PhaseNet] data_source=sds  MP reader  path={self.sds_path}")
        print(f"[PhaseNet] {len(tasks)} tasks  io_processes={n_proc}  "
              f"chunk={self.ann_chunk_s//3600}h  ann_batch={self.batch_size}")
        t_start_all = time.time()

        model = self._load_model()

        all_picks = []
        cnt = {"read": 0, "empty": 0, "annotated": 0, "ann_err": 0}

        ctx = mp.get_context("spawn")
        with ctx.Pool(processes=n_proc, initializer=_mp_init,
                      initargs=(str(self.sds_path), self.highpass,
                                self.ann_chunk_s)) as pool:
            for key, chunks in pool.imap_unordered(_mp_read_prep, tasks,
                                                   chunksize=1):
                if not chunks:
                    cnt["empty"] += 1
                else:
                    cnt["read"] += 1
                    for chunk in chunks:
                        try:
                            chunk = self._denoise_chunk(chunk)
                            with torch.no_grad():
                                ann = model.annotate(chunk,
                                                     batch_size=self.batch_size)
                            cnt["annotated"] += 1
                            df = self._extract_picks(key, ann, chunk)
                            if not df.empty:
                                all_picks.append(df)
                        except Exception as e:
                            logger.warning("annotate failed %s: %s", key, e)
                            cnt["ann_err"] += 1
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()

                done = cnt["read"] + cnt["empty"]
                if done % 10 == 0 or done == len(tasks):
                    picks_so_far = sum(len(d) for d in all_picks)
                    elapsed = time.time() - t_start_all
                    _dev_label = "GPU" if getattr(self, "_on_gpu", False) else "CPU"
                    sys.stdout.write(
                        f"\r  read={cnt['read']}/{len(tasks)} "
                        f"empty={cnt['empty']} {_dev_label}={cnt['annotated']} "
                        f"picks={picks_so_far}  {elapsed:.0f}s")
                    sys.stdout.flush()
        print()

        if not all_picks:
            print("[PhaseNet] No picks found.")
            return None

        result = pd.concat(all_picks, ignore_index=True)
        result.sort_values("phase_time", inplace=True)
        result.reset_index(drop=True, inplace=True)

        out = self.picks_dir / "picks.csv"
        result.to_csv(out, index=False)

        elapsed = time.time() - t_start_all
        p_count = len(result[result["phase_hint"] == "P"])
        s_count = len(result[result["phase_hint"] == "S"])
        print(f"[PhaseNet] Done (MP). {len(result)} picks"
              f" (P={p_count}, S={s_count})"
              f" → {out}  ({elapsed:.1f}s total)")
        return str(out)

    # =========================================================================
    # Public entry — batch pipeline (I/O → GPU → extract, no concurrent phases)
    # =========================================================================

    def run(self, workers: int = None):
        """Serial batches: read a batch -> GPU annotate -> extract, repeat.

        Why serial: seisbench annotate() may fork DataLoader workers; forking
        while a ThreadPool is active inherits a locked mutex -> SIGSEGV. So
        each phase runs to completion before the next. Memory is bounded by
        io_batch (= max(workers*4, 32)) streams per batch."""
        import torch

        n_w = workers or int(self.pcfg.get("workers", 4))

        # ── Parallel GPU workers: split tasks across N processes, then merge ──
        if self.gpu_workers > 1 and self._shard_id is None:
            return self._run_gpu_parallel(self.gpu_workers, n_w)

        # ── Multi-process SDS reader (I/O overlaps GPU) ───────────────────────
        if self.io_processes > 0 and self.data_source == "sds":
            return self._run_mp(self.io_processes)

        # streams per batch — enough parallelism for I/O, bounded memory
        io_batch = max(n_w * 4, 32)

        # ── select source ────────────────────────────────────────────────────
        ds = self.data_source
        if ds == "sds":
            tasks    = self._collect_sds_tasks()
            fetch_fn = self._fetch_sds
        elif ds == "sds_lite":
            tasks    = self._collect_sds_tasks()
            fetch_fn = self._fetch_sds_lite
        elif ds == "fdsn":
            tasks    = self._collect_fdsn_tasks()
            fetch_fn = self._fetch_fdsn
        elif ds == "sds+fdsn":
            tasks    = self._collect_fdsn_tasks()
            fetch_fn = self._fetch_sds_fdsn
        else:   # "file"
            tasks    = self._collect_files()
            fetch_fn = self._fetch_file

        # Shard worker: use the pre-built task subset handed by the parent.
        if self._tasks_override is not None:
            tasks = self._tasks_override

        if not tasks:
            print(f"[ERROR] No input found (data_source={ds})")
            sys.exit(1)

        label = {
            "file"    : f"file  dir={self.wave_dir}",
            "sds"     : f"SDS  path={self.sds_path}  (read-only)",
            "sds_lite": f"SDS-lite  path={self.sds_path}  "
                        f"(scan ringan + decode terfilter, by HakimBMKG)",
            "fdsn"    : f"FDSN  client={self.fdsn_client}"
                        + (f"  → save SDS={self.sds_path}" if self.fdsn_save_sds else ""),
            "sds+fdsn": f"SDS+FDSN  sds={self.sds_path}  fdsn={self.fdsn_client}"
                        + (f"  → save SDS" if self.fdsn_save_sds else ""),
        }.get(ds, ds)

        n_batches = (len(tasks) + io_batch - 1) // io_batch
        print(f"[PhaseNet] data_source={ds}  {label}")
        print(f"[PhaseNet] {len(tasks)} tasks  "
              f"io_batch={io_batch}  n_batches={n_batches}  "
              f"workers={n_w}  chunk={self.ann_chunk_s//3600}h  "
              f"ann_batch={self.batch_size}")
        t_start_all = time.time()

        model = self._load_model()

        cnt = {
            "fetched": 0, "fetch_err": 0,
            "annotated": 0, "ann_err": 0,
        }
        if self.fdsn_save_sds:
            cnt["saved_files"] = 0
        all_picks = []

        # ── fetch helper — called inside ThreadPoolExecutor only ─────────────
        def _fetch_one(task):
            key, st_raw = fetch_fn(task)
            if st_raw is None:
                return key, None
            if self.fdsn_save_sds:
                n = self._write_to_sds(st_raw, self.sds_path)
                cnt["saved_files"] += n
            return key, self._merge_raw(st_raw)

        # ── process tasks in batches ─────────────────────────────────────────
        for b_idx in range(0, len(tasks), io_batch):
            batch_tasks = tasks[b_idx : b_idx + io_batch]
            b_num = b_idx // io_batch + 1
            b_label = f"[B{b_num}/{n_batches}]"

            # ── Step 1: parallel I/O — all threads finish before GPU starts ──
            streams = {}   # key → merged Stream
            with ThreadPoolExecutor(max_workers=n_w) as pool:
                futs = {pool.submit(_fetch_one, t): t for t in batch_tasks}
                for fut in as_completed(futs):
                    try:
                        key, st = fut.result()
                    except Exception as e:
                        logger.warning("fetch error: %s", e)
                        cnt["fetch_err"] += 1
                        continue
                    if st is not None:
                        streams[key] = st
                        cnt["fetched"] += 1
                    else:
                        cnt["fetch_err"] += 1
                    _saved = (f"  saved={cnt['saved_files']}"
                              if self.fdsn_save_sds else "")
                    sys.stdout.write(
                        f"\r  {b_label} read {cnt['fetched']}"
                        f"/{len(tasks)}  err={cnt['fetch_err']}{_saved}"
                    )
                    sys.stdout.flush()
            # ThreadPool has exited — no threads active, GPU safe

            if not streams:
                print(f"\n  {b_label} no data")
                continue

            # ── Step 2: GPU annotation — no threads running ───────────────
            # annotations: (key, ann_stream, filtered_stream_ref); the stream
            # ref stays alive so Step 3 can measure amplitudes.
            annotations = []
            for key, st_raw in streams.items():
                try:
                    st = self._filter(st_raw)   # modifies in place, returns same obj
                    for chunk in self._slice_chunks(st):
                        chunk = self._denoise_chunk(chunk)
                        with torch.no_grad():
                            ann = model.annotate(chunk,
                                                 batch_size=self.batch_size)
                        annotations.append((key, ann, st))
                        cnt["annotated"] += 1
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception as e:
                    logger.warning("annotate failed %s: %s", key, e)
                    cnt["ann_err"] += 1
                _dev_label = "GPU" if getattr(self, "_on_gpu", False) else "CPU"
                sys.stdout.write(
                    f"\r  {b_label} {_dev_label} {cnt['annotated']} chunks"
                    f"  err={cnt['ann_err']}"
                )
                sys.stdout.flush()
            streams.clear()   # removes dict keys; st objects still held by annotations

            # ── Step 3: parallel pick extraction ─────────────────────────
            with ThreadPoolExecutor(max_workers=n_w) as pool:
                futs = {pool.submit(self._extract_picks, k, a, rs): k
                        for k, a, rs in annotations}
                for fut in as_completed(futs):
                    df = fut.result()
                    if not df.empty:
                        all_picks.append(df)
            annotations.clear()

            _picks_so_far = sum(len(d) for d in all_picks)
            _saved = (f"  saved={cnt['saved_files']}"
                      if self.fdsn_save_sds else "")
            elapsed = time.time() - t_start_all
            print(f"\r  {b_label} done  "
                  f"read={cnt['fetched']}  GPU={cnt['annotated']}"
                  f"  picks={_picks_so_far}{_saved}  {elapsed:.0f}s")

        # ── collect results ───────────────────────────────────────────────────
        if not all_picks:
            print("[PhaseNet] No picks found.")
            return None

        result = pd.concat(all_picks, ignore_index=True)
        result.sort_values("phase_time", inplace=True)
        result.reset_index(drop=True, inplace=True)

        out_name = (f"picks_shard_{self._shard_id}.csv"
                    if self._shard_id is not None else "picks.csv")
        out = self.picks_dir / out_name
        result.to_csv(out, index=False)

        elapsed = time.time() - t_start_all
        p_count = len(result[result["phase_hint"] == "P"])
        s_count = len(result[result["phase_hint"] == "S"])
        print(f"[PhaseNet] Done. {len(result)} picks"
              f" (P={p_count}, S={s_count})"
              f" → {out}  ({elapsed:.1f}s total)")
        return str(out)

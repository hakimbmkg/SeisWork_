"""
Minimal live seedlink session — by HakimBMKG

One active session at a time (consistent with the "one active config" pattern
in app.py / activeConfigId). Not a one-shot job like _pick_worker — this session
lives in its own daemon thread until stop() is called.

Per-stream buffers use a FIXED-size numpy ring buffer (pre-allocated,
overwritten circularly) — not a Python list/deque that keeps growing, since
this session lives a long time (unlike the app's other one-shot batch jobs).
This mirrors the scrttv pattern (a fixed-size C++ ring buffer), just a
pure-Python/numpy version without needing the seiscomp.io bindings.
"""

import io
import os
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
from obspy.clients.seedlink.easyseedlink import create_client


# _RingBuffer pre-allocates its FULL capacity per stream up front (see below) —
# this used to be 86400 (24h), which at ~475 mixed-rate streams measured ~30 GB
# RSS. SDS archiving (_write_sds) writes each packet straight to disk as it
# arrives and never reads from this buffer, so the 24h retention bought
# nothing there. The live dashboard only ever displays the last ~30 min
# (get_snapshot win_s=1900) and the default backfill window is also 30 min;
# event review beyond this window already falls back to reading SDS from disk
# (see the "ring buffer first, else SDS" paths in app.py). 1 hour keeps a
# comfortable margin over both without paying for a day of data nobody reads.
_BUFFER_SECONDS = 3600


def _segmentize(t: np.ndarray, v: np.ndarray, sr: float,
                bin_s: float) -> list[dict]:
    """Split (t, v) into CONTIGUOUS segments (no gaps), each segment decimated
    with a **min/max envelope per bin** (not by striding). Each bin_s-wide bin
    produces 2 consecutive values: min then max — so when the client draws a
    min→max vertical line per pixel, the result is a DENSE seismogram, not a
    thin line that loses wiggles to stride decimation. This is exactly scrttv's
    RecordPolyline::pushRecord technique (1 min-max vertical line per pixel column).

    A gap (a time jump > 1.5× the nominal interval) starts a new segment, so each
    point's time stays correct (t = t0 + i*step PER segment) and real acquisition
    gaps are drawn as a break — not a straight line forcibly joining them.

    Per-segment format: {"t0": epoch seconds, "step": seconds/point, "vs": [int]}.
    `vs` holds [min0,max0, min1,max1, …] pairs spaced step = bin_s/2 apart.
    """
    n = len(t)
    if n == 0:
        return []
    dt_nom = (1.0 / sr) if (sr and sr > 0) else (
        float(np.median(np.diff(t))) if n > 1 else 1.0)
    if dt_nom <= 0:
        dt_nom = 1.0
    # Each new segment starts at an index where the time jump > 1.5× nominal
    if n > 1:
        breaks = np.where(np.diff(t) > 1.5 * dt_nom)[0] + 1
        bounds = np.concatenate(([0], breaks, [n]))
    else:
        bounds = np.array([0, n])
    bin_n     = max(1, int(round(bin_s / dt_nom)))   # samples per bin
    half_step = (bin_n * dt_nom) / 2.0               # spacing between (min,max) points
    out = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        sv = v[a:b]
        m = len(sv)
        if m == 0:
            continue
        nb  = (m + bin_n - 1) // bin_n               # jumlah bin (ceil)
        pad = nb * bin_n - m
        padded = np.concatenate([sv, np.full(pad, sv[-1])]) if pad else sv
        mat   = padded.reshape(nb, bin_n)
        vmins = mat.min(axis=1)
        vmaxs = mat.max(axis=1)
        inter = np.empty(nb * 2, dtype=np.int64)     # interleave min,max
        inter[0::2] = vmins
        inter[1::2] = vmaxs
        out.append({
            "t0":   float(t[a]),
            "step": float(half_step),
            "vs":   [int(x) for x in inter],
        })
    return out


class _RingBuffer:
    """Fixed-size numpy ring buffer: overwrites circularly, O(1) per write,
    no reallocation/popleft scan even when the session runs for hours."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.times = np.full(capacity, np.nan, dtype=np.float64)
        self.vals = np.full(capacity, np.nan, dtype=np.float64)
        self._write = 0
        self._filled = False

    def extend(self, t: np.ndarray, v: np.ndarray) -> None:
        n = len(t)
        if n >= self.capacity:
            self.times[:] = t[-self.capacity:]
            self.vals[:] = v[-self.capacity:]
            self._write = 0
            self._filled = True
            return
        end = self._write + n
        if end <= self.capacity:
            self.times[self._write:end] = t
            self.vals[self._write:end] = v
        else:
            k = self.capacity - self._write
            self.times[self._write:] = t[:k]
            self.vals[self._write:] = v[:k]
            self.times[:end - self.capacity] = t[k:]
            self.vals[:end - self.capacity] = v[k:]
            self._filled = True
        self._write = end % self.capacity

    def snapshot(self) -> tuple[np.ndarray, np.ndarray]:
        if not self._filled:
            t, v = self.times[:self._write], self.vals[:self._write]
        else:
            order = np.r_[self._write:self.capacity, 0:self._write]
            t, v = self.times[order], self.vals[order]
        mask = ~np.isnan(t)
        t, v = t[mask], v[mask]
        # Time-order defense: backfill (old data) can be written CONCURRENTLY
        # with live (new data) → one out-of-order "seam" in the buffer. Cheap O(n)
        # check; sort only when truly non-monotonic (transient, until the old data
        # slides out). Consumers (segmentize/get_window) need ascending t.
        if t.size > 1 and not (np.diff(t) >= 0).all():
            idx = np.argsort(t, kind="mergesort")
            t, v = t[idx], v[idx]
        return t, v


class LiveSeedlinkSession:
    def __init__(self):
        self._lock = threading.Lock()
        self._client = None
        self._thread: threading.Thread | None = None
        self._buffers: dict[str, _RingBuffer] = {}
        self.host = None
        self.port = None
        self.n_packets = 0
        self.last_data_time = None
        self.connected = False
        self.error = None
        self.inventory_path: str | None = None  # primary inventory XML in use
        self.inventory_paths: list[str] = []     # ALL inventory XMLs (multi-seedlink)
        self.sources: list[dict] = []            # [{host, port, n_streams}] one per seedlink source
        self._clients: list = []                 # one easyseedlink client per source
        self._src_connected: dict[int, bool] = {}
        self.cfg_id: str | None = None          # active config (for the per-config catalog)
        self.include_accel: bool = False        # subscribe to HN/BN/EN/... (accelerometer) too
        self.session_epoch: float | None = None  # set at start() —
        # clients use it to detect server restarts (changed → re-snapshot)
        self._picks: dict[str, list[dict]] = {}  # "NET.STA" -> [{t, phase}, ...]
        self._pick_log: deque = deque(maxlen=500)  # global log in arrival order
        self._n_picks_total: int = 0               # session-total counter (never wraps)
        self._sr: dict[str, float] = {}  # key -> sampling rate (used by the spectrogram)
        self.backfill_active = False     # True while the initial backfill runs
        self.backfill_done = False       # True once the initial backfill finished
        self.backfill_window_s = 0.0     # requested backfill window (seconds)
        self.backfill_n_streams = 0      # number of streams that got backfill data
        self._running_flag = False       # gate to cancel the backfill on stop()
        # On-disk SDS roots read at (re)start to refill the ring buffer from the
        # slarchive archive (work/online_sds and/or the SeisComP SDS) — this is
        # what makes a restart resume from history instead of an empty panel.
        self._backfill_sds_paths: list[str] = []
        # Built-in SDS (fallback when slarchive is not on PATH)
        self._sds_path: str | None = None
        self._sds_written: int = 0       # total traces successfully written to SDS
        self._sds_errors: int = 0        # traces that failed to write (IO/encoding error)
        # Spectrograms are pre-computed in the background — the HTTP endpoint returns
        # the cache directly, no on-demand compute (FFT ~0.1 s/stream → timeout).
        self._spec_cache: dict[str, dict] = {}   # key -> spec dict
        self._spec_thread: threading.Thread | None = None
        # Which keys the frontend actually asked about recently (visible rows
        # only, per _visibleStreamKeys() on the client) — the background loop
        # only computes these instead of every stream, which used to cost an
        # FFT per stream per cycle regardless of whether anyone could see it.
        self._spec_wanted_keys: set[str] = set()
        self._spec_wanted_at: float = 0.0

    def _ingest_trace(self, trace, count_packet: bool = True):
        """Write 1 trace into its stream's ring buffer. Used by _on_data (live)
        AND _backfill (historical). count_packet=False during backfill so the
        packet stats (n_packets/last_data_time) only reflect the live stream."""
        key = f"{trace.stats.network}.{trace.stats.station}.{trace.stats.location}.{trace.stats.channel}"
        t0 = trace.stats.starttime.timestamp
        dt = trace.stats.delta
        times = t0 + np.arange(trace.stats.npts) * dt
        vals = trace.data.astype(np.float64)
        with self._lock:
            buf = self._buffers.get(key)
            if buf is None:
                capacity = int(_BUFFER_SECONDS * trace.stats.sampling_rate * 1.1) + 1
                buf = self._buffers[key] = _RingBuffer(capacity)
            buf.extend(times, vals)
            self._sr[key] = trace.stats.sampling_rate
            if count_packet:
                self.n_packets += 1
                self.last_data_time = trace.stats.endtime.isoformat()

    def _write_sds(self, trace):
        """Write 1 trace into the local SDS via ObsPy (fallback when slarchive is absent).
        SDS format: ROOT/YEAR/NET/STA/CHA.D/NET.STA.LOC.CHA.D.YEAR.DOY — appended."""
        from obspy import Stream
        try:
            t   = trace.stats.starttime
            net = trace.stats.network
            sta = trace.stats.station
            loc = trace.stats.location or ""
            cha = trace.stats.channel
            fname = (Path(self._sds_path) / str(t.year) / net / sta /
                     f"{cha}.D" / f"{net}.{sta}.{loc}.{cha}.D.{t.year}.{t.julday:03d}")
            fname.parent.mkdir(parents=True, exist_ok=True)
            buf = io.BytesIO()
            Stream([trace]).write(buf, format="MSEED", reclen=512, encoding="STEIM2")
            buf.seek(0)
            with open(str(fname), "ab") as f:
                f.write(buf.read())
            self._sds_written += 1
        except Exception:
            self._sds_errors += 1

    def set_sds_path(self, path: str | None):
        """Aktifkan/nonaktifkan penulisan SDS built-in. path=None → matikan."""
        self._sds_path = path or None
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)

    def _on_data(self, trace):
        self._ingest_trace(trace, count_packet=True)
        if self._sds_path:
            self._write_sds(trace)

    def _backfill(self, host, port, streams, seconds: float):
        """Fill the ring buffer with the last ~`seconds` seconds via a SeedLink
        time-window (dial-up), BEFORE the live stream starts. Purpose: when the
        SeisWork server restarts, the time window (e.g. 30 minutes) is immediately
        filled with historical data — it doesn't start empty. Runs in the startup
        thread, one pass per stream, with a soft deadline so it doesn't delay live
        data too long when the SeedLink server is slow.

        The ring buffer assumes time-ordered writes, so the backfill (old data)
        MUST finish before live (new data) starts writing — that's why this is
        called from _startup_and_run before _run()."""
        from obspy import UTCDateTime
        from obspy.clients.seedlink.basic_client import Client as _SLClient

        self.backfill_active = True
        self.backfill_done = False
        self.backfill_window_s = float(seconds)
        deadline = time.time() + 90.0   # total backfill deadline (background thread)
        try:
            cli = _SLClient(host, int(port), timeout=12)
        except Exception as exc:
            self.backfill_active = False
            self.backfill_done = True
            self.error = f"backfill init: {exc}"
            return

        end = UTCDateTime()
        start = end - float(seconds)
        n_ok = 0
        for net, sta, loc, cha in streams:
            if not self._running_flag or time.time() > deadline:
                break
            # cha is already resolved to a single exact band (see _run()) —
            # fetch exactly that, plus the accelerometer band too when opted in.
            selectors = [cha]
            if self.include_accel and len(cha) == 3:
                selectors.append(f"?N{cha[-1]}")
            got = False
            for selector in selectors:
                try:
                    st = cli.get_waveforms(net, sta, loc or "", selector, start, end)
                except Exception:
                    continue
                for tr in (st or []):
                    if tr.stats.npts > 0:
                        self._ingest_trace(tr, count_packet=False)
                        got = True
            if got:
                n_ok += 1
        self.backfill_active = False
        self.backfill_done = True
        self.backfill_n_streams = n_ok

    def _backfill_from_sds(self, streams, seconds: float) -> int:
        """Fill the ring buffer with the last ~`seconds` from the ON-DISK SDS
        archive (slarchive's work/online_sds and/or the SeisComP SDS) BEFORE the
        live stream starts. This is what makes a SeisWork restart resume from
        history: the SeedLink server itself often serves only a short (or no)
        time-window, but slarchive has been writing every packet to disk, so the
        display can be reconstructed from there. Returns the number of streams
        that yielded data. Safe/best-effort: a missing archive or a read error
        for one stream never aborts the rest."""
        roots = [p for p in (self._backfill_sds_paths or []) if p and Path(p).is_dir()]
        if not roots:
            return 0
        try:
            from obspy import UTCDateTime
            from obspy.clients.filesystem.sds import Client as _SDSReadClient
        except Exception:
            return 0
        end = UTCDateTime()
        start = end - float(seconds)
        clients = []
        for r in roots:
            try:
                clients.append(_SDSReadClient(r))
            except Exception:
                pass
        n_ok = 0
        for net, sta, loc, cha in streams:
            if not self._running_flag:
                break
            selectors = [cha]
            if self.include_accel and len(cha) == 3:
                selectors.append(f"?N{cha[-1]}")
            got = False
            for cli in clients:
                for selector in selectors:
                    try:
                        # loc '*' matches whatever location code the archive used
                        # (stations mix '' and '00'); wildcards are supported by the
                        # SDS client. cha is already the resolved single band.
                        st = cli.get_waveforms(net, sta, loc or "*", selector, start, end)
                    except Exception:
                        continue
                    for tr in (st or []):
                        if tr.stats.npts > 0:
                            self._ingest_trace(tr, count_packet=False)
                            got = True
                if got:
                    break   # first archive that has this stream wins
            if got:
                n_ok += 1
        return n_ok

    def _startup_and_run(self, host, port, streams, backfill_seconds, idx: int = 0):
        # Backfill RUNS IN PARALLEL with live — NEVER block live (a SeedLink server
        # can be slow / lack time-window support → serialized, live would freeze
        # for tens of seconds & the panel would go blank). The ring buffer is safe against
        # out-of-order seams because snapshot() sorts defensively (see _RingBuffer.snapshot).
        if backfill_seconds and backfill_seconds > 0:
            def _bf():
                self.backfill_active = True
                self.backfill_done = False
                try:
                    # 1) On-disk SDS (slarchive) FIRST — reliable history across a
                    #    SeisWork restart even when the SeedLink server serves no
                    #    time-window. Only the primary source (idx 0) reads SDS to
                    #    avoid every extra source re-reading the same archive.
                    n_sds = 0
                    if idx == 0:
                        n_sds = self._backfill_from_sds(streams, backfill_seconds)
                    # 2) SeedLink time-window — supplements the most-recent gap the
                    #    SDS on disk may not have flushed yet (and covers servers
                    #    with no slarchive at all).
                    self._backfill(host, port, streams, backfill_seconds)
                    if n_sds:
                        self.backfill_n_streams = max(self.backfill_n_streams, n_sds)
                except Exception as exc:   # a failed backfill must not disturb live
                    self.error = f"backfill: {exc}"
                finally:
                    self.backfill_active = False
                    self.backfill_done = True
            threading.Thread(target=_bf, daemon=True,
                             name=f"seedlink-backfill-{idx}").start()
        self._run(host, port, streams, idx)

    def _run(self, host, port, streams, idx: int = 0):
        # One client per seedlink source; all feed the same ring buffers via
        # _on_data. self.connected = True while ANY source is connected.
        try:
            client = create_client(f"{host}:{port}", on_data=self._on_data)
            for net, sta, loc, cha in streams:
                # cha is already resolved to a SINGLE exact band (see
                # resolve_live_channels() in slinktool_verify.py, applied by
                # /api/online/connect before calling start()) — subscribe to
                # it exactly, not a wildcard, so a station with several
                # simultaneously-live bands (e.g. BH + HH + SH all at once)
                # doesn't get every one of them.
                client.select_stream(net, sta, cha)
                if self.include_accel and len(cha) == 3:
                    # Accelerometer bands use instrument code 'N' — a
                    # different letter from the resolved seismometer channel
                    # above, so this never collides/duplicates it. No
                    # per-station "best accel band" resolution exists yet,
                    # so this stays a wildcard match (opt-in feature, off by
                    # default).
                    client.select_stream(net, sta, f"?N{cha[-1]}")
            with self._lock:
                self._clients.append(client)
            if idx == 0:
                self._client = client
            self._src_connected[idx] = True
            self.connected = True
            client.run()  # blocks until client.close() / conn loss
        except Exception as exc:
            self.error = f"[{host}:{port}] {exc}"
        finally:
            self._src_connected[idx] = False
            self.connected = any(self._src_connected.values())

    def start(self, host: str, port: int, streams: list[tuple[str, str, str, str]],
              inventory_path: str | None = None,
              backfill_seconds: float = 1800.0,
              cfg_id: str | None = None,
              extra_sources: list[dict] | None = None,
              inventory_paths: list[str] | None = None,
              include_accel: bool = False,
              backfill_sds_paths: list[str] | None = None) -> bool:
        """Start a new session. Return False (no-op) when the target (host:port),
        the inventory set, AND the accelerometer toggle all match the currently
        running session — seedlink concept: an already-filled buffer is NOT
        wiped just because the user reopens the same dashboard (e.g. switching
        Online/Offline tabs and back). A different host/port, a re-uploaded
        inventory XML (e.g. station positions changed), or a flipped
        include_accel triggers a full restart so the change actually applies.

        backfill_seconds: on (re)start, first fill the last N seconds from the
        SeedLink time-window so the display doesn't start empty (default
        1800 = 30 minutes, matching the display window). 0 = skip the backfill.

        extra_sources: additional SeedLink servers feeding the SAME session —
        [{host, port, streams:[(net,sta,loc,cha), ...]}]. Each source gets its
        own client thread; all data lands in the shared ring buffers, so more
        stations/networks can stream in than a single server provides.
        inventory_paths: every inventory XML backing the sources (primary +
        extras); consumers that need station metadata merge all of them.
        include_accel: subscribe to accelerometer bands (HN/BN/EN/...) too —
        default False since PhaseNet-style pickers are tuned on velocity
        (seismometer) data, not acceleration. Applies to every station."""
        extra_sources = list(extra_sources or [])
        new_targets = sorted([(host, int(port))] +
                             [(s["host"], int(s["port"])) for s in extra_sources])
        new_inventory_paths = [p for p in (inventory_paths or
                               ([inventory_path] if inventory_path else [])) if p]
        if (self.connected and getattr(self, "_targets", None) == new_targets
                and new_inventory_paths == self.inventory_paths
                and bool(include_accel) == self.include_accel):
            if cfg_id:
                # Cheap metadata update even on resume — otherwise a session that
                # lost its cfg_id (e.g. an old client reconnecting without it)
                # could never be re-tagged without a full, buffer-wiping restart.
                self.cfg_id = cfg_id
            return False
        self.stop()
        self._targets = new_targets
        self.host, self.port = host, port
        self.n_packets = 0
        self.last_data_time = None
        self.error = None
        self._buffers = {}
        self._picks = {}
        self._pick_log.clear()
        self._n_picks_total = 0
        self.backfill_active = False
        self.backfill_done = False
        self.backfill_window_s = float(backfill_seconds or 0)
        self.backfill_n_streams = 0
        self.session_epoch = time.time()
        self.inventory_path = inventory_path
        self.inventory_paths = new_inventory_paths
        self.include_accel = bool(include_accel)
        self._backfill_sds_paths = [p for p in (backfill_sds_paths or []) if p]
        self.sources = ([{"host": host, "port": int(port), "n_streams": len(streams)}] +
                        [{"host": s["host"], "port": int(s["port"]),
                          "n_streams": len(s.get("streams") or [])}
                         for s in extra_sources])
        self._clients = []
        self._src_connected = {}
        self.cfg_id = cfg_id or None
        self._running_flag = True
        self._spec_cache = {}
        self._thread = threading.Thread(
            target=self._startup_and_run,
            args=(host, port, streams, float(backfill_seconds or 0), 0), daemon=True
        )
        self._thread.start()
        for i, src in enumerate(extra_sources, start=1):
            s_streams = [tuple(x) for x in (src.get("streams") or [])]
            threading.Thread(
                target=self._startup_and_run,
                args=(src["host"], int(src["port"]), s_streams,
                      float(backfill_seconds or 0), i),
                daemon=True, name=f"seedlink-src{i}",
            ).start()
        self._spec_thread = threading.Thread(target=self._bg_spec_loop, daemon=True)
        self._spec_thread.start()
        return True

    def _bg_spec_loop(self):
        """Background thread: pre-compute the spectrogram for the streams the
        frontend actually has visible, every 10s. The HTTP endpoint returns
        the cache directly → response <10ms, never times out.

        Computing this for EVERY stream regardless of visibility used to cost
        one FFT per stream per cycle even when only a handful of rows were
        ever on screen — at ~470 streams that's the dominant CPU cost of the
        whole process. _spec_wanted_keys (set by get_all_spectrograms when
        the frontend asks for specific keys) tracks what's actually visible;
        it goes stale after 60s (client gone/idle) and falls back to a small
        bounded default so a freshly (re)started session still has *something*
        cached before the frontend's first poll arrives."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        while self._running_flag:
            time.sleep(10)
            if not self._running_flag:
                break
            with self._lock:
                all_keys = list(self._buffers.keys())
            if not all_keys:
                continue
            wanted_fresh = (time.time() - self._spec_wanted_at) < 60.0
            if wanted_fresh and self._spec_wanted_keys:
                keys = [k for k in all_keys if k in self._spec_wanted_keys]
            else:
                keys = all_keys[:20]
            if not keys:
                continue

            def _compute(key):
                net, sta, loc, cha = key.split(".")
                return self.get_spectrogram(net, sta, loc, cha, window_s=1800.0)

            new_cache = {}
            with ThreadPoolExecutor(max_workers=min(len(keys), 6)) as pool:
                futures = {pool.submit(_compute, k): k for k in keys}
                for fut in as_completed(futures):
                    if not self._running_flag:
                        break
                    spec = fut.result()
                    if spec:
                        new_cache[spec["key"]] = spec
            if self._running_flag:
                self._spec_cache = new_cache

    def stop(self):
        self._running_flag = False
        with self._lock:
            clients = list(self._clients)
            self._clients = []
        if self._client is not None and self._client not in clients:
            clients.append(self._client)
        for c in clients:
            try:
                c.close()
            except Exception:
                pass
        self._client = None
        self._src_connected = {}
        self.connected = False
        self._sds_path = None

    def status(self) -> dict:
        return {
            "connected"        : self.connected,
            "cfg_id"           : self.cfg_id,
            "include_accel"    : self.include_accel,
            "host"             : self.host,
            "port"             : self.port,
            "n_packets"        : self.n_packets,
            "last_data_time"   : self.last_data_time,
            "error"            : self.error,
            "streams"          : sorted(self._buffers.keys()),
            "inventory_path"   : self.inventory_path,
            "inventory_paths"  : list(self.inventory_paths),
            "sources"          : [dict(s, connected=bool(self._src_connected.get(i)))
                                  for i, s in enumerate(self.sources)],
            "session_epoch"    : self.session_epoch,
            "backfill_active"  : self.backfill_active,
            "backfill_done"    : self.backfill_done,
            "backfill_window_s": self.backfill_window_s,
            "backfill_n_streams": self.backfill_n_streams,
            "sds_path"         : self._sds_path,
            "sds_written"      : self._sds_written,
            "sds_errors"       : self._sds_errors,
        }

    def add_pick(self, net: str, sta: str, t: float, phase: str, source: str = "scautopick"):
        """Called from the /api/online/trigger endpoint (scautopick bridge) OR
        from RealtimePicker (PhaseNet, see _realtime_pipeline.py). Stored
        per net.sta — shown on every channel row of that station, capped
        so it doesn't grow unbounded over a long live session. `source` is used
        by the frontend to color-code it (scautopick vs phasenet).
        Also appended to the global _pick_log (500-entry ring buffer) for the Pick Log View."""
        key = f"{net}.{sta}"
        with self._lock:
            lst = self._picks.setdefault(key, [])
            # Dedup — the bridge/picker may re-deliver the same pick (restart, retry,
            # window overlap between PhaseNet cycles)
            if any(abs(p["t"] - t) < 0.3 and p["phase"] == phase for p in lst):
                return
            lst.append({"t": t, "phase": phase, "source": source})
            if len(lst) > 50:
                del lst[: len(lst) - 50]
            # Find the best channel for the pick-log entry (Z channel preferred)
            cha = ""
            for k in self._buffers:
                parts = k.split(".")
                if len(parts) == 4 and parts[0] == net and parts[1] == sta:
                    if not cha or parts[3].endswith("Z"):
                        cha = parts[3]
            self._pick_log.append({
                "t": t, "net": net, "sta": sta, "cha": cha,
                "phase": phase, "source": source,
            })
            self._n_picks_total += 1

    def get_pick_log(self, since: float = 0.0, n: int = 200) -> list[dict]:
        """Most recent picks since `since` epoch, at most `n`, ascending order."""
        with self._lock:
            items = [p for p in self._pick_log if p["t"] > since]
        return items[-n:] if len(items) > n else items

    def list_stream_keys(self) -> list[str]:
        """Every active stream key (NET.STA.LOC.CHA) — used by RealtimePicker
        to know which stations have live data to pick from."""
        with self._lock:
            return list(self._buffers.keys())

    def get_window(self, net: str, sta: str, loc: str, cha: str,
                   seconds: float) -> tuple[np.ndarray, np.ndarray]:
        """RAW (non-downsampled) window of the last N seconds — unlike get_buffer(),
        which downsamples to ~1000 points (fine for plotting, NOT enough for
        PhaseNet inference, which needs the native sample rate). Used by RealtimePicker."""
        key, buf, _sr = self._resolve_key(net, sta, loc, cha)
        if buf is None:
            return np.array([]), np.array([])
        with self._lock:
            t, v = buf.snapshot()
        if len(t) == 0:
            return np.array([]), np.array([])
        mask = t >= t[-1] - seconds
        return t[mask], v[mask]

    def get_sampling_rate(self, net: str, sta: str, loc: str, cha: str) -> float | None:
        _key, _buf, sr = self._resolve_key(net, sta, loc, cha)
        return sr

    def get_all_buffers(self, max_pts: int = 500) -> list[dict]:
        """Snapshot every active stream at once — used by the live waveform panel
        that shows all stations stacked (scrttv style), not one at a time."""
        with self._lock:
            items = list(self._buffers.items())
            picks_snap = {k: list(v) for k, v in self._picks.items()}
        out = []
        for key, buf in items:
            net, sta, loc, cha = key.split(".")
            t, v = buf.snapshot()
            if len(t) == 0:
                continue
            step = max(1, len(t) // max_pts)
            out.append({
                "key": key, "net": net, "sta": sta, "loc": loc, "cha": cha,
                "points": [{"t": float(tt), "v": float(vv)}
                           for tt, vv in zip(t[::step], v[::step])],
                "picks": picks_snap.get(f"{net}.{sta}", []),
            })
        return out

    def get_snapshot(self, bin_s: float = 1.0, win_s: float = 1900.0) -> list[dict]:
        """Initial snapshot on connect — each stream is sent as a list of CONTIGUOUS
        segments (see _segmentize) with a min/max envelope per bin_s-second bin.
        win_s: trims to the last N seconds. After the snapshot, the frontend uses
        get_delta(cursors), which only sends new data per stream.

        Each stream also carries `last_t` (the last sample's DATA time) so the
        client can use it as the next delta cursor — not the wall clock."""
        with self._lock:
            items = list(self._buffers.items())
            picks_snap = {k: list(v) for k, v in self._picks.items()}
            sr_snap = dict(self._sr)
        out = []
        for key, buf in items:
            net, sta, loc, cha = key.split(".")
            t, v = buf.snapshot()
            if len(t) == 0:
                continue
            # Trim to the latest window — avoids a huge step when the buffer is full
            if win_s > 0 and len(t) > 1:
                mask = t >= t[-1] - win_s
                t, v = t[mask], v[mask]
            if len(t) == 0:
                continue
            sr = float(sr_snap.get(key, 0))
            segments = _segmentize(t, v, sr, bin_s)
            if not segments:
                continue
            out.append({
                "key": key, "net": net, "sta": sta, "loc": loc, "cha": cha,
                "sr"      : sr,
                "segments": segments,
                "last_t"  : float(t[-1]),
                "picks"   : picks_snap.get(f"{net}.{sta}", []),
            })
        return out

    def get_delta(self, cursors: dict[str, float],
                  bin_s: float = 1.0, win_s: float = 1900.0) -> tuple[list[dict], float]:
        """New samples since the PER-STREAM cursor (cursors[key] = the last sample
        DATA time the client already has). This is the key to a gap-free waveform:
        the cursor is data-time based, not the server wall clock — so seedlink
        latency (data arriving a few seconds late) never makes the delta skip
        over or drop data. Similar to scrttv resuming from _lastRecordTime.

        A stream the client doesn't know yet (missing from cursors) is sent the
        full latest window, so newly appearing stations show up without needing
        a full re-snapshot. Each stream carries `last_t` (the new cursor)."""
        srv_time = time.time()
        with self._lock:
            items = list(self._buffers.items())
            picks_snap = {k: list(v) for k, v in self._picks.items()}
            sr_snap = dict(self._sr)
        out = []
        for key, buf in items:
            net, sta, loc, cha = key.split(".")
            t, v = buf.snapshot()
            if len(t) == 0:
                continue
            cur = cursors.get(key)
            if cur is None:
                # Stream new to this client → send the full latest window
                if win_s > 0 and len(t) > 1:
                    m = t >= t[-1] - win_s
                    t_new, v_new = t[m], v[m]
                else:
                    t_new, v_new = t, v
            else:
                m = t > cur
                t_new, v_new = t[m], v[m]
            sr = float(sr_snap.get(key, 0))
            picks_new = [p for p in picks_snap.get(f"{net}.{sta}", [])
                         if cur is None or p["t"] > cur - 5]
            if len(t_new) == 0 and not picks_new:
                continue
            entry: dict = {
                "key": key, "net": net, "sta": sta, "loc": loc, "cha": cha,
                "sr": sr, "picks_new": picks_new,
            }
            if len(t_new) > 0:
                entry["segments_new"] = _segmentize(t_new, v_new, sr, bin_s)
                entry["last_t"]       = float(t_new[-1])
            out.append(entry)
        return out, srv_time

    def get_spectrogram(self, net: str, sta: str, loc: str, cha: str,
                         window_s: float = 1800.0,
                         max_t_bins: int = 180, max_f_bins: int = 48) -> dict | None:
        """Live spectrogram spanning the ENTIRE display window (default 30 minutes) —
        so its strip stretches the full width below the waveform, not just a
        chunk on the right. Kept lightweight because:
          - overlap is reduced to 50% (half as many FFTs as a 75% overlap),
          - Sxx is downsampled to max_t_bins × max_f_bins (bounded payload, ~30-60 KB),
          - it's lazy-loaded, called only for visible rows (see the caller)."""
        window_s = min(window_s, 1810.0)   # upper bound = the 30-minute display window
        key, buf, sr = self._resolve_key(net, sta, loc, cha)
        if buf is None or not sr:
            return None
        with self._lock:
            t, v = buf.snapshot()
        if len(t) == 0:
            return None
        mask = t >= t[-1] - window_s
        t, v = t[mask], v[mask]
        if len(v) < int(sr * 2):
            return None

        from scipy.signal import spectrogram as _spgram
        nperseg  = min(int(sr * 2), 256, len(v))
        noverlap = nperseg // 2          # 50% — half the FFTs vs 75%, visually sufficient
        f, st, Sxx = _spgram(v, fs=sr, nperseg=nperseg, noverlap=noverlap, scaling="density")
        Sxx_db = 10 * np.log10(Sxx + 1e-30)
        f_max  = min(25.0, sr / 2)
        f_mask = f <= f_max
        f_out, S_out = f[f_mask], Sxx_db[f_mask, :]
        # Absolute time axis: NEVER use `t[0] + st` (assumes samples contiguous at
        # `sr`). The ring buffer can have overlaps/gaps from reconnects → `len(v)/sr`
        # differs from the real time span, making the spectrogram MISALIGNED with the
        # waveform (the "offside" bug). Map each column to the REAL sample timestamp via
        # the segment-center index (`st*sr`), so the spectrogram time axis = data time axis.
        seg_idx = np.clip(np.round(st * sr).astype(int), 0, len(t) - 1)
        t_out = t[seg_idx]  # absolute epoch from the actual sample timestamps

        # Downsample to max_t_bins × max_f_bins. On the time axis take the MAX per
        # bin (not a stride) so short transients (earthquakes) are not lost when a
        # long window is compressed into ~180 columns.
        nf, nt = S_out.shape
        t_step = max(1, nt // max_t_bins)
        f_step = max(1, nf // max_f_bins)
        if t_step > 1:
            nt2 = (nt // t_step) * t_step
            S_t = S_out[:, :nt2].reshape(nf, nt2 // t_step, t_step).max(axis=2)
            t_small = t_out[:nt2:t_step]
        else:
            S_t, t_small = S_out, t_out
        S_small = S_t[::f_step, :]
        f_small = f_out[::f_step]

        return {
            "key": key, "freqs": f_small.tolist(),
            "times": [float(x) for x in t_small],
            "Sxx": np.round(S_small, 1).tolist(),   # 1 decimal is enough → ~½ payload
            "vmin": float(np.percentile(S_small, 5)) if S_small.size else 0.0,
            "vmax": float(np.percentile(S_small, 99)) if S_small.size else 1.0,
        }

    def get_all_spectrograms(self, window_s: float = 1800.0,
                              only_keys: set[str] | None = None) -> list[dict]:
        """Return the spectrogram from the background cache (_bg_spec_loop).
        Response <10ms — no on-demand computation happens here."""
        if only_keys:
            # Record what the frontend actually wants — _bg_spec_loop uses
            # this to avoid computing a spectrogram for every stream when
            # only a handful are ever visible at once.
            self._spec_wanted_keys = set(only_keys)
            self._spec_wanted_at = time.time()
        cache = self._spec_cache
        if only_keys:
            return [v for k, v in cache.items() if k in only_keys]
        return list(cache.values())

    def _resolve_key(self, net: str, sta: str, loc: str, cha: str):
        key = f"{net}.{sta}.{loc}.{cha}"
        with self._lock:
            if key in self._buffers:
                return key, self._buffers[key], self._sr.get(key)
            prefix, suffix = f"{net}.{sta}.", f".{cha}"
            for k in self._buffers:
                if k.startswith(prefix) and k.endswith(suffix):
                    return k, self._buffers[k], self._sr.get(k)
        return key, None, None

    def get_buffer(self, net: str, sta: str, loc: str, cha: str) -> list[dict]:
        # Try the exact key first, then fall back to loc variations (a server may send "00" despite a "" subscription)
        key = f"{net}.{sta}.{loc}.{cha}"
        with self._lock:
            buf = self._buffers.get(key)
            if buf is None:
                # Find a buffer with the same net.sta and channel, any loc
                prefix = f"{net}.{sta}."
                suffix = f".{cha}"
                candidates = [k for k in self._buffers if k.startswith(prefix) and k.endswith(suffix)]
                if candidates:
                    buf = self._buffers[candidates[0]]
                    key = candidates[0]
            if buf is None:
                return []
            t, v = buf.snapshot()
        if len(t) == 0:
            return []
        # downsample to a max of ~1000 points so it stays cheap to plot
        step = max(1, len(t) // 1000)
        return [{"t": float(tt), "v": float(vv)} for tt, vv in zip(t[::step], v[::step])]


_LIVE_SESSION = LiveSeedlinkSession()

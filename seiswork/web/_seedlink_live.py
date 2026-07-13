"""
Minimal live seedlink session - by HakimBMKG

Each connected config (cfg_id) gets its own isolated bucket of ring buffers,
picks, and connection state, all inside one `LiveSeedlinkSession` object
(`_LIVE_SESSION` below) whose identity never changes: app.py and
_realtime_pipeline.py import that object once at module load, so rebinding
it to a different object per config would strand existing references on a
stale object. `self.cfg_id` is the in-memory "currently active" pointer
(mirrors _realtime_pipeline.py's `_ACTIVE_CFG_ID`); every accessor takes an
optional trailing `cfg_id` and falls back to it when omitted, so old call
sites that don't know about per-config buckets keep working unchanged.

Per-stream buffers use a fixed-size numpy ring buffer (pre-allocated,
overwritten circularly), not a growing Python list/deque, since this
session runs for a long time (unlike the app's other one-shot batch jobs).
This mirrors the scrttv pattern (a fixed-size C++ ring buffer), in pure
Python/numpy without needing the seiscomp.io bindings.
"""

import io
import os
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
from obspy.clients.seedlink.easyseedlink import create_client


# _RingBuffer pre-allocates its full capacity per stream up front. This used
# to be 86400 (24h), which at ~475 mixed-rate streams measured ~30 GB RSS.
# SDS archiving (_write_sds) writes each packet straight to disk and never
# reads this buffer, so the 24h retention bought nothing. The live dashboard
# only shows the last ~30 min (get_snapshot win_s=1900) and backfill is also
# 30 min; older data falls back to reading SDS from disk (see the "ring
# buffer first, else SDS" paths in app.py). 1 hour keeps a comfortable
# margin without paying for a day of data nobody reads.
_BUFFER_SECONDS = 3600


def _segmentize(t: np.ndarray, v: np.ndarray, sr: float,
                bin_s: float) -> list[dict]:
    """Split (t, v) into contiguous segments (no gaps), each decimated with a
    min/max envelope per bin (not by striding). Each bin_s-wide bin produces
    2 values, min then max, so a min-max vertical line per pixel gives a
    dense seismogram instead of a thin line that loses wiggles to stride
    decimation. Same technique as scrttv's RecordPolyline::pushRecord (1
    min-max vertical line per pixel column).

    A gap (time jump > 1.5x the nominal interval) starts a new segment, so
    each point's time stays correct per segment, and real acquisition gaps
    show as a break instead of a straight line joining them.

    Per-segment format: {"t0": epoch seconds, "step": seconds/point, "vs": [int]}.
    `vs` holds [min0,max0, min1,max1, ...] pairs spaced step = bin_s/2 apart.
    """
    n = len(t)
    if n == 0:
        return []
    dt_nom = (1.0 / sr) if (sr and sr > 0) else (
        float(np.median(np.diff(t))) if n > 1 else 1.0)
    if dt_nom <= 0:
        dt_nom = 1.0
    # Each new segment starts at an index where the time jump > 1.5x nominal
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
        nb  = (m + bin_n - 1) // bin_n               # bin count (ceil)
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
        # Time-order defense: backfill (old data) can be written concurrently
        # with live (new data), causing one out-of-order "seam" in the buffer.
        # Cheap O(n) check; sort only when truly non-monotonic (transient,
        # until the old data slides out). Consumers (segmentize/get_window)
        # need ascending t.
        if t.size > 1 and not (np.diff(t) >= 0).all():
            idx = np.argsort(t, kind="mergesort")
            t, v = t[idx], v[idx]
        return t, v


class _SessionState:
    """Per-cfg_id connection/session state. What used to be scalar attributes
    directly on LiveSeedlinkSession (self.connected, self.host,
    self.session_epoch, ...) now lives here instead, one instance per
    connected config, so two configs never share mutable state."""

    __slots__ = (
        "targets", "subscribed", "host", "port",
        "inventory_path", "inventory_paths", "include_accel",
        "connected", "session_epoch", "error",
        "n_packets", "last_data_time", "sources",
        "client", "clients", "src_connected",
        "backfill_active", "backfill_done", "backfill_window_s",
        "backfill_n_streams", "backfill_sds_paths",
        "sds_path", "sds_written", "sds_errors",
        "running_flag", "thread", "spec_thread",
    )

    def __init__(self):
        self.targets: list | None = None
        self.subscribed: frozenset | None = None
        self.host = None
        self.port = None
        self.inventory_path: str | None = None
        self.inventory_paths: list[str] = []
        self.include_accel: bool = False
        self.connected = False
        self.session_epoch: float | None = None
        self.error = None
        self.n_packets = 0
        self.last_data_time = None
        self.sources: list[dict] = []
        self.client = None
        self.clients: list = []
        self.src_connected: dict[int, bool] = {}
        self.backfill_active = False
        self.backfill_done = False
        self.backfill_window_s = 0.0
        self.backfill_n_streams = 0
        self.backfill_sds_paths: list[str] = []
        self.sds_path: str | None = None
        self.sds_written = 0
        self.sds_errors = 0
        self.running_flag = False
        self.thread: threading.Thread | None = None
        self.spec_thread: threading.Thread | None = None


# Shared read-only default returned when a bucket hasn't been created yet
# (e.g. status()/get_snapshot() called before any connect ever happened).
# Never mutated, only read.
_DEFAULT_SESSION_STATE = _SessionState()


class LiveSeedlinkSession:
    def __init__(self):
        self._lock = threading.Lock()
        self.cfg_id: str | None = None  # active-config pointer (mirrors
        # _realtime_pipeline.py's _ACTIVE_CFG_ID). Set to whichever cfg most
        # recently called start(); accessors called without an explicit
        # cfg_id resolve to this one, so old callers keep the "one active
        # session" behavior even though this file now supports several
        # concurrent configs.
        self._sess: dict[str, _SessionState] = {}
        # All per-stream state, nested one level deeper by cfg_id bucket
        # ("" = the legacy/no-cfg bucket, mirrors _catalog_path's fallback
        # to the un-partitioned root):
        self._buffers: dict[str, dict[str, _RingBuffer]] = {}
        self._picks: dict[str, dict[str, list[dict]]] = {}   # cid -> "NET.STA" -> [{t, phase}, ...]
        self._pick_log: dict[str, deque] = {}                # cid -> global log in arrival order
        self._n_picks_total_by_cid: dict[str, int] = {}      # cid -> session-total counter
        self._sr: dict[str, dict[str, float]] = {}           # cid -> key -> sampling rate
        self._spec_cache: dict[str, dict[str, dict]] = {}    # cid -> key -> spec dict
        self._spec_wanted_keys: dict[str, set[str]] = {}     # cid -> wanted keys
        self._spec_wanted_at: dict[str, float] = {}          # cid -> last-wanted timestamp

    # ── Per-cfg_id resolution ────────────────────────────────────────────────
    def _cid(self, cfg_id: str | None) -> str:
        """Resolve which cfg_id bucket a call targets: explicit arg wins,
        else the in-memory active pointer, else the un-partitioned legacy
        bucket (""). Mirrors _realtime_pipeline.py's _catalog_path
        resolution (`cfg_id or _ACTIVE_CFG_ID or get_active_cfg_id(...)`)."""
        return cfg_id or self.cfg_id or ""

    def _sess_for(self, cid: str) -> "_SessionState":
        """Get-or-create the SessionState for a bucket (mutating access)."""
        sess = self._sess.get(cid)
        if sess is None:
            sess = self._sess[cid] = _SessionState()
        return sess

    def _active(self) -> "_SessionState":
        """Read-only view of the currently-active config's state. Never
        creates a bucket, falls back to a shared empty default."""
        return self._sess.get(self.cfg_id or "", _DEFAULT_SESSION_STATE)

    # ── Backward-compat read-only properties ────────────────────────────────
    # External code (app.py, _realtime_pipeline.py) reads these as bare
    # attributes on `_LIVE_SESSION`, e.g. `_LIVE_SESSION.connected`. Keeping
    # them as properties over the active config's state means all those
    # ~15 call sites keep working unchanged.
    @property
    def connected(self) -> bool:
        return self._active().connected

    @property
    def host(self):
        return self._active().host

    @property
    def port(self):
        return self._active().port

    @property
    def inventory_path(self):
        return self._active().inventory_path

    @property
    def inventory_paths(self):
        return self._active().inventory_paths

    @property
    def session_epoch(self):
        return self._active().session_epoch

    @property
    def _n_picks_total(self):
        return self._n_picks_total_by_cid.get(self._cid(None), 0)

    @property
    def _sds_path(self):
        return self._active().sds_path

    def _ingest_trace(self, trace, cfg_id: str | None = None, count_packet: bool = True):
        """Write 1 trace into its stream's ring buffer, in the given config's
        bucket. Used by _on_data (live) and _backfill (historical).
        count_packet=False during backfill so the packet stats
        (n_packets/last_data_time) only reflect the live stream."""
        cid = self._cid(cfg_id)
        key = f"{trace.stats.network}.{trace.stats.station}.{trace.stats.location}.{trace.stats.channel}"
        t0 = trace.stats.starttime.timestamp
        dt = trace.stats.delta
        times = t0 + np.arange(trace.stats.npts) * dt
        vals = trace.data.astype(np.float64)
        with self._lock:
            bucket = self._buffers.setdefault(cid, {})
            buf = bucket.get(key)
            if buf is None:
                capacity = int(_BUFFER_SECONDS * trace.stats.sampling_rate * 1.1) + 1
                buf = bucket[key] = _RingBuffer(capacity)
            buf.extend(times, vals)
            self._sr.setdefault(cid, {})[key] = trace.stats.sampling_rate
            if count_packet:
                sess = self._sess_for(cid)
                sess.n_packets += 1
                sess.last_data_time = trace.stats.endtime.isoformat()

    def _write_sds(self, trace, cid: str):
        """Write 1 trace into the local SDS via ObsPy (fallback when slarchive is absent).
        SDS format: ROOT/YEAR/NET/STA/CHA.D/NET.STA.LOC.CHA.D.YEAR.DOY, appended."""
        from obspy import Stream
        sess = self._sess_for(cid)
        try:
            t   = trace.stats.starttime
            net = trace.stats.network
            sta = trace.stats.station
            loc = trace.stats.location or ""
            cha = trace.stats.channel
            fname = (Path(sess.sds_path) / str(t.year) / net / sta /
                     f"{cha}.D" / f"{net}.{sta}.{loc}.{cha}.D.{t.year}.{t.julday:03d}")
            fname.parent.mkdir(parents=True, exist_ok=True)
            buf = io.BytesIO()
            Stream([trace]).write(buf, format="MSEED", reclen=512, encoding="STEIM2")
            buf.seek(0)
            with open(str(fname), "ab") as f:
                f.write(buf.read())
            sess.sds_written += 1
        except Exception:
            sess.sds_errors += 1

    def set_sds_path(self, path: str | None, cfg_id: str | None = None):
        """Enable/disable built-in SDS writing. path=None disables it."""
        cid = self._cid(cfg_id)
        sess = self._sess_for(cid)
        sess.sds_path = path or None
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)

    def _on_data(self, trace, cid: str):
        self._ingest_trace(trace, cfg_id=cid, count_packet=True)
        sess = self._sess_for(cid)
        if sess.sds_path:
            self._write_sds(trace, cid)

    def _backfill(self, host, port, streams, seconds: float, cid: str):
        """Fill the ring buffer with the last ~`seconds` seconds via a SeedLink
        time-window (dial-up), before the live stream starts. Purpose: when the
        SeisWork server restarts, the display window is immediately filled with
        historical data instead of starting empty. Runs in the startup thread,
        one pass per stream, with a soft deadline so it doesn't delay live data
        too long when the SeedLink server is slow.

        The ring buffer assumes time-ordered writes, so backfill (old data)
        must finish before live (new data) starts writing; that's why this is
        called from _startup_and_run before _run()."""
        from obspy import UTCDateTime
        from obspy.clients.seedlink.basic_client import Client as _SLClient

        sess = self._sess_for(cid)
        sess.backfill_active = True
        sess.backfill_done = False
        sess.backfill_window_s = float(seconds)
        deadline = time.time() + 90.0   # total backfill deadline (background thread)
        try:
            cli = _SLClient(host, int(port), timeout=12)
        except Exception as exc:
            sess.backfill_active = False
            sess.backfill_done = True
            sess.error = f"backfill init: {exc}"
            return

        end = UTCDateTime()
        start = end - float(seconds)
        n_ok = 0
        for net, sta, loc, cha in streams:
            if not sess.running_flag or time.time() > deadline:
                break
            # cha is already resolved to a single exact band (see _run());
            # fetch exactly that, plus the accelerometer band too when opted in.
            selectors = [cha]
            if sess.include_accel and len(cha) == 3:
                selectors.append(f"?N{cha[-1]}")
            got = False
            for selector in selectors:
                try:
                    st = cli.get_waveforms(net, sta, loc or "", selector, start, end)
                except Exception:
                    continue
                for tr in (st or []):
                    if tr.stats.npts > 0:
                        self._ingest_trace(tr, cfg_id=cid, count_packet=False)
                        got = True
            if got:
                n_ok += 1
        sess.backfill_active = False
        sess.backfill_done = True
        sess.backfill_n_streams = n_ok

    def _backfill_from_sds(self, streams, seconds: float, cid: str) -> int:
        """Fill the ring buffer with the last ~`seconds` from the on-disk SDS
        archive (slarchive's work/online_sds and/or the SeisComP SDS), before
        the live stream starts. This is what lets a SeisWork restart resume
        from history: the SeedLink server itself often serves only a short
        (or no) time-window, but slarchive has been writing every packet to
        disk, so the display can be reconstructed from there. Returns the
        number of streams that yielded data. Best-effort: a missing archive
        or a read error for one stream never aborts the rest."""
        sess = self._sess_for(cid)
        roots = [p for p in (sess.backfill_sds_paths or []) if p and Path(p).is_dir()]
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
            if not sess.running_flag:
                break
            selectors = [cha]
            if sess.include_accel and len(cha) == 3:
                selectors.append(f"?N{cha[-1]}")
            got = False
            for cli in clients:
                for selector in selectors:
                    try:
                        # loc '*' matches whatever location code the archive
                        # used (stations mix '' and '00'); the SDS client
                        # supports wildcards. cha is already the resolved
                        # single band.
                        st = cli.get_waveforms(net, sta, loc or "*", selector, start, end)
                    except Exception:
                        continue
                    for tr in (st or []):
                        if tr.stats.npts > 0:
                            self._ingest_trace(tr, cfg_id=cid, count_packet=False)
                            got = True
                if got:
                    break   # first archive that has this stream wins
            if got:
                n_ok += 1
        return n_ok

    def _startup_and_run(self, host, port, streams, backfill_seconds, idx: int, cid: str):
        # Backfill runs in parallel with live and must never block it (a
        # SeedLink server can be slow or lack time-window support; if
        # serialized, live would freeze for tens of seconds and the panel
        # would go blank). The ring buffer is safe against out-of-order
        # seams because snapshot() sorts defensively (see _RingBuffer.snapshot).
        sess = self._sess_for(cid)
        if backfill_seconds and backfill_seconds > 0:
            def _bf():
                sess.backfill_active = True
                sess.backfill_done = False
                try:
                    # 1) On-disk SDS (slarchive) first: reliable history across
                    #    a SeisWork restart even when the SeedLink server serves
                    #    no time-window. Only the primary source (idx 0) reads
                    #    SDS, to avoid every extra source re-reading the archive.
                    n_sds = 0
                    if idx == 0:
                        n_sds = self._backfill_from_sds(streams, backfill_seconds, cid)
                    # 2) SeedLink time-window: supplements the most-recent gap
                    #    the SDS on disk may not have flushed yet (and covers
                    #    servers with no slarchive at all).
                    self._backfill(host, port, streams, backfill_seconds, cid)
                    if n_sds:
                        sess.backfill_n_streams = max(sess.backfill_n_streams, n_sds)
                except Exception as exc:   # a failed backfill must not disturb live
                    sess.error = f"backfill: {exc}"
                finally:
                    sess.backfill_active = False
                    sess.backfill_done = True
            threading.Thread(target=_bf, daemon=True,
                             name=f"seedlink-backfill-{cid}-{idx}").start()
        self._run(host, port, streams, idx, cid)

    def _run(self, host, port, streams, idx: int, cid: str):
        # One client per seedlink source; all feed the same ring buffers via
        # _on_data. sess.connected = True while any source is connected.
        sess = self._sess_for(cid)
        try:
            client = create_client(f"{host}:{port}",
                                   on_data=lambda tr: self._on_data(tr, cid))
            for net, sta, loc, cha in streams:
                # cha is already resolved to a single exact band (see
                # resolve_live_channels() in slinktool_verify.py, applied by
                # /api/online/connect before calling start()). Subscribe to
                # it exactly, not a wildcard, so a station with several
                # simultaneously-live bands (e.g. BH + HH + SH all at once)
                # doesn't get every one of them.
                client.select_stream(net, sta, cha)
                if sess.include_accel and len(cha) == 3:
                    # Accelerometer bands use instrument code 'N', a
                    # different letter from the resolved seismometer channel
                    # above, so this never collides/duplicates it. No
                    # per-station "best accel band" resolution exists yet,
                    # so this stays a wildcard match (opt-in feature, off by
                    # default).
                    client.select_stream(net, sta, f"?N{cha[-1]}")
            with self._lock:
                sess.clients.append(client)
            if idx == 0:
                sess.client = client
            sess.src_connected[idx] = True
            sess.connected = True
            client.run()  # blocks until client.close() / conn loss
        except Exception as exc:
            sess.error = f"[{host}:{port}] {exc}"
        finally:
            sess.src_connected[idx] = False
            sess.connected = any(sess.src_connected.values())

    def start(self, host: str, port: int, streams: list[tuple[str, str, str, str]],
              inventory_path: str | None = None,
              backfill_seconds: float = 1800.0,
              cfg_id: str | None = None,
              extra_sources: list[dict] | None = None,
              inventory_paths: list[str] | None = None,
              include_accel: bool = False,
              backfill_sds_paths: list[str] | None = None) -> bool:
        """Start a session for `cfg_id` (or the currently active one when
        omitted). Returns False (no-op) when host:port, inventory set,
        accelerometer toggle, cfg_id, and the subscribed station set all
        match the currently running session for that bucket, so an
        already-filled buffer isn't wiped just from reopening the same
        dashboard (e.g. switching Online/Offline tabs). A change in any of
        those (different host/port, re-uploaded inventory, flipped
        include_accel, a genuinely different cfg_id, or a different station
        set) triggers a full restart of that bucket only; other configs'
        buckets are untouched.

        backfill_seconds: on (re)start, first fill the last N seconds from
        the SeedLink time-window so the display doesn't start empty (default
        1800 = 30 min, matching the display window). 0 skips the backfill.

        extra_sources: additional SeedLink servers feeding the same session:
        [{host, port, streams:[(net,sta,loc,cha), ...]}]. Each gets its own
        client thread; all data lands in this cfg_id's ring buffers, so more
        stations/networks can stream in than one server provides.
        inventory_paths: every inventory XML backing the sources (primary +
        extras); consumers needing station metadata merge all of them.
        include_accel: also subscribe to accelerometer bands (HN/BN/EN/...),
        default False since PhaseNet-style pickers are tuned on velocity
        (seismometer) data, not acceleration. Applies to every station."""
        extra_sources = list(extra_sources or [])
        new_targets = sorted([(host, int(port))] +
                             [(s["host"], int(s["port"])) for s in extra_sources])
        new_inventory_paths = [p for p in (inventory_paths or
                               ([inventory_path] if inventory_path else [])) if p]
        new_subscribed = frozenset(
            tuple(s) for s in streams
        ) | frozenset(
            tuple(s) for src in extra_sources for s in (src.get("streams") or [])
        )

        cid = self._cid(cfg_id)
        sess = self._sess.get(cid)
        # A resume is only safe when nothing about the subscription actually
        # changed. Two conditions used to be missing here, letting a second
        # config silently take over the first's live subscription instead of
        # re-subscribing to its own:
        #   - cfg_id: switching configs is never a silent resume, even with
        #     the same station set, since cfg_id must be re-tagged downstream
        #     too. An empty cfg_id on either side (client hasn't sent one, or
        #     session lost its label) still allows a resume, so a missing
        #     label can be healed without wiping live buffers.
        #   - the subscribed station set: same host/inventory/accel but a
        #     different (possibly bbox-filtered) `streams` argument (e.g. a
        #     second region-limited config on the same SeedLink server) must
        #     trigger a real re-subscribe, not just a relabel.
        cfg_changed = bool(self.cfg_id) and bool(cfg_id) and (self.cfg_id != cfg_id)
        if (sess is not None and sess.connected and sess.targets == new_targets
                and new_inventory_paths == sess.inventory_paths
                and bool(include_accel) == sess.include_accel
                and not cfg_changed
                and sess.subscribed == new_subscribed):
            # Cheap metadata update even on resume: otherwise a session that
            # lost its cfg_id (e.g. an old client reconnecting without it)
            # could never be re-tagged without a full, buffer-wiping restart.
            self.cfg_id = cid or None
            return False

        self.stop(cfg_id=cid)
        sess = self._sess_for(cid)
        sess.targets = new_targets
        sess.subscribed = new_subscribed
        sess.host, sess.port = host, port
        sess.n_packets = 0
        sess.last_data_time = None
        sess.error = None
        self._buffers[cid] = {}
        self._picks[cid] = {}
        self._pick_log[cid] = deque(maxlen=500)
        self._n_picks_total_by_cid[cid] = 0
        self._sr[cid] = {}
        self._spec_cache[cid] = {}
        self._spec_wanted_keys[cid] = set()
        self._spec_wanted_at[cid] = 0.0
        sess.backfill_active = False
        sess.backfill_done = False
        sess.backfill_window_s = float(backfill_seconds or 0)
        sess.backfill_n_streams = 0
        sess.session_epoch = time.time()
        sess.inventory_path = inventory_path
        sess.inventory_paths = new_inventory_paths
        sess.include_accel = bool(include_accel)
        sess.backfill_sds_paths = [p for p in (backfill_sds_paths or []) if p]
        sess.sources = ([{"host": host, "port": int(port), "n_streams": len(streams)}] +
                        [{"host": s["host"], "port": int(s["port"]),
                          "n_streams": len(s.get("streams") or [])}
                         for s in extra_sources])
        sess.clients = []
        sess.src_connected = {}
        self.cfg_id = cid or None
        sess.running_flag = True
        sess.thread = threading.Thread(
            target=self._startup_and_run,
            args=(host, port, streams, float(backfill_seconds or 0), 0, cid), daemon=True
        )
        sess.thread.start()
        for i, src in enumerate(extra_sources, start=1):
            s_streams = [tuple(x) for x in (src.get("streams") or [])]
            threading.Thread(
                target=self._startup_and_run,
                args=(src["host"], int(src["port"]), s_streams,
                      float(backfill_seconds or 0), i, cid),
                daemon=True, name=f"seedlink-src{i}-{cid}",
            ).start()
        sess.spec_thread = threading.Thread(target=self._bg_spec_loop, args=(cid,), daemon=True)
        sess.spec_thread.start()
        return True

    def _bg_spec_loop(self, cid: str):
        """Background thread: pre-compute the spectrogram every 10s for the
        streams the frontend actually has visible, per cfg_id bucket. The
        HTTP endpoint just returns the cache, so response is <10ms and never
        times out.

        Computing this for every stream regardless of visibility used to cost
        one FFT per stream per cycle even with only a handful on screen; at
        ~470 streams that dominated CPU. _spec_wanted_keys (set by
        get_all_spectrograms when the frontend asks for specific keys) tracks
        what's actually visible; it goes stale after 60s (client gone/idle)
        and falls back to a small bounded default so a freshly (re)started
        session still has something cached before the frontend's first poll."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        sess = self._sess_for(cid)
        while sess.running_flag:
            time.sleep(10)
            if not sess.running_flag:
                break
            with self._lock:
                all_keys = list(self._buffers.get(cid, {}).keys())
            if not all_keys:
                continue
            wanted_fresh = (time.time() - self._spec_wanted_at.get(cid, 0.0)) < 60.0
            wanted_keys = self._spec_wanted_keys.get(cid) or set()
            if wanted_fresh and wanted_keys:
                keys = [k for k in all_keys if k in wanted_keys]
            else:
                keys = all_keys[:20]
            if not keys:
                continue

            def _compute(key):
                net, sta, loc, cha = key.split(".")
                return self.get_spectrogram(net, sta, loc, cha, window_s=1800.0, cfg_id=cid)

            new_cache = {}
            with ThreadPoolExecutor(max_workers=min(len(keys), 6)) as pool:
                futures = {pool.submit(_compute, k): k for k in keys}
                for fut in as_completed(futures):
                    if not sess.running_flag:
                        break
                    spec = fut.result()
                    if spec:
                        new_cache[spec["key"]] = spec
            if sess.running_flag:
                self._spec_cache[cid] = new_cache

    def stop(self, cfg_id: str | None = None):
        cid = self._cid(cfg_id)
        sess = self._sess.get(cid)
        if sess is None:
            return
        sess.running_flag = False
        with self._lock:
            clients = list(sess.clients)
            sess.clients = []
        if sess.client is not None and sess.client not in clients:
            clients.append(sess.client)
        for c in clients:
            try:
                c.close()
            except Exception:
                pass
        sess.client = None
        sess.src_connected = {}
        sess.connected = False
        sess.sds_path = None

    def status(self, cfg_id: str | None = None) -> dict:
        cid = self._cid(cfg_id)
        sess = self._sess.get(cid, _DEFAULT_SESSION_STATE)
        return {
            "connected"        : sess.connected,
            "cfg_id"           : cid or None,
            "include_accel"    : sess.include_accel,
            "host"             : sess.host,
            "port"             : sess.port,
            "n_packets"        : sess.n_packets,
            "last_data_time"   : sess.last_data_time,
            "error"            : sess.error,
            "streams"          : sorted(self._buffers.get(cid, {}).keys()),
            "inventory_path"   : sess.inventory_path,
            "inventory_paths"  : list(sess.inventory_paths),
            "sources"          : [dict(s, connected=bool(sess.src_connected.get(i)))
                                  for i, s in enumerate(sess.sources)],
            "session_epoch"    : sess.session_epoch,
            "backfill_active"  : sess.backfill_active,
            "backfill_done"    : sess.backfill_done,
            "backfill_window_s": sess.backfill_window_s,
            "backfill_n_streams": sess.backfill_n_streams,
            "sds_path"         : sess.sds_path,
            "sds_written"      : sess.sds_written,
            "sds_errors"       : sess.sds_errors,
        }

    def add_pick(self, net: str, sta: str, t: float, phase: str,
                 source: str = "scautopick", cfg_id: str | None = None):
        """Called from the /api/online/trigger endpoint (scautopick bridge) OR
        from RealtimePicker (PhaseNet, see _realtime_pipeline.py). Stored
        per net.sta — shown on every channel row of that station, capped
        so it doesn't grow unbounded over a long live session. `source` is used
        by the frontend to color-code it (scautopick vs phasenet).
        Also appended to that cfg_id's _pick_log (500-entry ring buffer) for the Pick Log View."""
        cid = self._cid(cfg_id)
        key = f"{net}.{sta}"
        with self._lock:
            picks_bucket = self._picks.setdefault(cid, {})
            lst = picks_bucket.setdefault(key, [])
            # Dedup — the bridge/picker may re-deliver the same pick (restart, retry,
            # window overlap between PhaseNet cycles)
            if any(abs(p["t"] - t) < 0.3 and p["phase"] == phase for p in lst):
                return
            lst.append({"t": t, "phase": phase, "source": source})
            if len(lst) > 50:
                del lst[: len(lst) - 50]
            # Find the best channel for the pick-log entry (Z channel preferred)
            cha = ""
            for k in self._buffers.get(cid, {}):
                parts = k.split(".")
                if len(parts) == 4 and parts[0] == net and parts[1] == sta:
                    if not cha or parts[3].endswith("Z"):
                        cha = parts[3]
            pick_log = self._pick_log.setdefault(cid, deque(maxlen=500))
            pick_log.append({
                "t": t, "net": net, "sta": sta, "cha": cha,
                "phase": phase, "source": source,
            })
            self._n_picks_total_by_cid[cid] = self._n_picks_total_by_cid.get(cid, 0) + 1

    def get_pick_log(self, since: float = 0.0, n: int = 200,
                      cfg_id: str | None = None) -> list[dict]:
        """Most recent picks since `since` epoch, at most `n`, ascending order."""
        cid = self._cid(cfg_id)
        with self._lock:
            pick_log = self._pick_log.get(cid) or ()
            items = [p for p in pick_log if p["t"] > since]
        return items[-n:] if len(items) > n else items

    def get_n_picks_total(self, cfg_id: str | None = None) -> int:
        """Session-total pick counter for a specific config (never wraps)."""
        return self._n_picks_total_by_cid.get(self._cid(cfg_id), 0)

    def list_stream_keys(self, cfg_id: str | None = None) -> list[str]:
        """Every active stream key (NET.STA.LOC.CHA) — used by RealtimePicker
        to know which stations have live data to pick from."""
        cid = self._cid(cfg_id)
        with self._lock:
            return list(self._buffers.get(cid, {}).keys())

    def get_window(self, net: str, sta: str, loc: str, cha: str,
                   seconds: float, cfg_id: str | None = None) -> tuple[np.ndarray, np.ndarray]:
        """RAW (non-downsampled) window of the last N seconds — unlike get_buffer(),
        which downsamples to ~1000 points (fine for plotting, NOT enough for
        PhaseNet inference, which needs the native sample rate). Used by RealtimePicker."""
        cid = self._cid(cfg_id)
        key, buf, _sr = self._resolve_key(net, sta, loc, cha, cid)
        if buf is None:
            return np.array([]), np.array([])
        with self._lock:
            t, v = buf.snapshot()
        if len(t) == 0:
            return np.array([]), np.array([])
        mask = t >= t[-1] - seconds
        return t[mask], v[mask]

    def get_sampling_rate(self, net: str, sta: str, loc: str, cha: str,
                           cfg_id: str | None = None) -> float | None:
        cid = self._cid(cfg_id)
        _key, _buf, sr = self._resolve_key(net, sta, loc, cha, cid)
        return sr

    def get_all_buffers(self, max_pts: int = 500, cfg_id: str | None = None) -> list[dict]:
        """Snapshot every active stream at once — used by the live waveform panel
        that shows all stations stacked (scrttv style), not one at a time."""
        cid = self._cid(cfg_id)
        with self._lock:
            items = list(self._buffers.get(cid, {}).items())
            picks_snap = {k: list(v) for k, v in self._picks.get(cid, {}).items()}
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

    def get_snapshot(self, bin_s: float = 1.0, win_s: float = 1900.0,
                      cfg_id: str | None = None) -> list[dict]:
        """Initial snapshot on connect — each stream is sent as a list of CONTIGUOUS
        segments (see _segmentize) with a min/max envelope per bin_s-second bin.
        win_s: trims to the last N seconds. After the snapshot, the frontend uses
        get_delta(cursors), which only sends new data per stream.

        Each stream also carries `last_t` (the last sample's DATA time) so the
        client can use it as the next delta cursor — not the wall clock."""
        cid = self._cid(cfg_id)
        with self._lock:
            items = list(self._buffers.get(cid, {}).items())
            picks_snap = {k: list(v) for k, v in self._picks.get(cid, {}).items()}
            sr_snap = dict(self._sr.get(cid, {}))
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
                  bin_s: float = 1.0, win_s: float = 1900.0,
                  cfg_id: str | None = None) -> tuple[list[dict], float]:
        """New samples since the PER-STREAM cursor (cursors[key] = the last sample
        DATA time the client already has). This is the key to a gap-free waveform:
        the cursor is data-time based, not the server wall clock — so seedlink
        latency (data arriving a few seconds late) never makes the delta skip
        over or drop data. Similar to scrttv resuming from _lastRecordTime.

        A stream the client doesn't know yet (missing from cursors) is sent the
        full latest window, so newly appearing stations show up without needing
        a full re-snapshot. Each stream carries `last_t` (the new cursor)."""
        cid = self._cid(cfg_id)
        srv_time = time.time()
        with self._lock:
            items = list(self._buffers.get(cid, {}).items())
            picks_snap = {k: list(v) for k, v in self._picks.get(cid, {}).items()}
            sr_snap = dict(self._sr.get(cid, {}))
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
                         max_t_bins: int = 180, max_f_bins: int = 48,
                         cfg_id: str | None = None) -> dict | None:
        """Live spectrogram spanning the ENTIRE display window (default 30 minutes) —
        so its strip stretches the full width below the waveform, not just a
        chunk on the right. Kept lightweight because:
          - overlap is reduced to 50% (half as many FFTs as a 75% overlap),
          - Sxx is downsampled to max_t_bins × max_f_bins (bounded payload, ~30-60 KB),
          - it's lazy-loaded, called only for visible rows (see the caller)."""
        cid = self._cid(cfg_id)
        window_s = min(window_s, 1810.0)   # upper bound = the 30-minute display window
        key, buf, sr = self._resolve_key(net, sta, loc, cha, cid)
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
        # Never use `t[0] + st` for the time axis: reconnect gaps/overlaps in
        # the ring buffer make `len(v)/sr` differ from the real time span,
        # misaligning the spectrogram with the waveform. Map each column to
        # its real sample timestamp via the segment-center index instead.
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
                              only_keys: set[str] | None = None,
                              cfg_id: str | None = None) -> list[dict]:
        """Return the spectrogram from the background cache (_bg_spec_loop).
        Response <10ms — no on-demand computation happens here."""
        cid = self._cid(cfg_id)
        if only_keys:
            # Record what the frontend actually wants — _bg_spec_loop uses
            # this to avoid computing a spectrogram for every stream when
            # only a handful are ever visible at once.
            self._spec_wanted_keys[cid] = set(only_keys)
            self._spec_wanted_at[cid] = time.time()
        cache = self._spec_cache.get(cid, {})
        if only_keys:
            return [v for k, v in cache.items() if k in only_keys]
        return list(cache.values())

    def _resolve_key(self, net: str, sta: str, loc: str, cha: str, cid: str):
        key = f"{net}.{sta}.{loc}.{cha}"
        with self._lock:
            bucket = self._buffers.get(cid, {})
            sr_bucket = self._sr.get(cid, {})
            if key in bucket:
                return key, bucket[key], sr_bucket.get(key)
            prefix, suffix = f"{net}.{sta}.", f".{cha}"
            for k in bucket:
                if k.startswith(prefix) and k.endswith(suffix):
                    return k, bucket[k], sr_bucket.get(k)
        return key, None, None

    def get_buffer(self, net: str, sta: str, loc: str, cha: str,
                   cfg_id: str | None = None) -> list[dict]:
        # Exact key first, then loc variations (a server may send "00" for a "" subscription)
        cid = self._cid(cfg_id)
        key = f"{net}.{sta}.{loc}.{cha}"
        with self._lock:
            bucket = self._buffers.get(cid, {})
            buf = bucket.get(key)
            if buf is None:
                # Find a buffer with the same net.sta and channel, any loc
                prefix = f"{net}.{sta}."
                suffix = f".{cha}"
                candidates = [k for k in bucket if k.startswith(prefix) and k.endswith(suffix)]
                if candidates:
                    buf = bucket[candidates[0]]
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

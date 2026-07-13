#!/usr/bin/env python3
"""
SeisWork - SDS Lite Reader (lightweight MiniSEED window reader)
Author : HakimBMKG

Pattern from BotSeiscompListener/bin/BotListener.py (`_process_and_repick`):
instead of `obspy.read()` on the full file, it opens a SeisComP RecordStream
and iterates records one at a time, touching only the requested time range,
via native SeisComP I/O (C++).

`seiscomp.io` can't be imported in the `seiswork` env (ABI mismatch with the
conda libpython, see mseedlite.py). This module clones the approach instead:

  1. `mseedlite` (pure-Python port of seiscomp's mseedlite.py; struct.unpack
     only, no STEIM decode, no global C state, so thread-safe) scans each
     record's header (time, size) in the SDS day-file without building a Trace.
  2. Raw bytes of records overlapping [t_start, t_end] are collected.
  3. ObsPy decodes only that filtered subset, not the full day.

I/O note: the rotational HDD is the real bottleneck (seek-thrashing under
concurrent random reads), so each file is read once, sequentially, into
memory, then filtered there - no per-record disk seeks.

BENCHMARK (notebooks/_test_sds_lite.ipynb, 8 Jun 2026, 7G.SP06..HHZ):
  Correctness: GOOD - samples byte-identical to SDSClient (1 day & 1 hour).
  Speed: BAD for bulk reads (per station-day):
                1 day : SDSClient 0.34s   vs  lite 1.53s   (4.5x slower)
                1 hour: SDSClient 0.02s   vs  lite 0.85s   (40x  slower)

  Reason: `obspy.clients.filesystem.sds.Client` already does selective reads
  at the libmseed (C) level via starttime/endtime kwargs, it doesn't decode
  a full day as assumed. `mseedlite.Input` (pure Python) still builds a full
  Record object for all ~70000 records in a 36 MB day-file regardless of
  relevance, and that Python loop is far slower than C.

  Conclusion: BotListener's record-by-record pattern is correct and light,
  but its speed comes from native compiled I/O plus a small window
  (seconds, not a whole station-day). Cloning it in pure Python for bulk
  load is a net loss. Kept here (opt-in, `data_source: "sds_lite"`) as a
  reference and for small/sparse windows, but not for bulk production
  picking; `data_source: "sds"` (SDSClient) remains the fastest path.
"""

import io
import glob
import logging
import threading
from pathlib import Path

from . import mseedlite as mseed

logger = logging.getLogger(__name__)

# The final ObsPy/libmseed decode step (filtered subset only) uses a
# C-extension that isn't thread-safe for concurrent reads - same idea as
# _MSEED_LOCK in phasenet.py, but the critical section is much shorter
# (decoding a small subset, not the whole file).
_DECODE_LOCK = threading.Lock()


class SDSLiteReader:
    """Read a time window net.sta.loc.chan from an SDS archive, lightweight.

    Unlike obspy.clients.filesystem.sds.Client.get_waveforms (which decodes
    every record in the day-file into a full Trace, then filters by time),
    this reader scans first (pure Python, no decode), then decodes only the
    relevant bytes.

    Drop-in replacement for `SDSClient(...).get_waveforms(...)`, returns
    an `obspy.Stream`.
    """

    def __init__(self, sds_root):
        self.sds_root = Path(sds_root)

    # ------------------------------------------------------------------
    def get_waveforms(self, net, sta, loc, chan, t_start, t_end):
        """Return an obspy.Stream for [t_start, t_end], lightweight decode."""
        from obspy import Stream, UTCDateTime, read as obs_read

        t0, t1 = UTCDateTime(t_start), UTCDateTime(t_end)
        paths = self._find_files(net, sta, loc, chan, t0, t1)

        st = Stream()
        for path in paths:
            blob = self._filter_records(path, t0, t1)
            if not blob:
                continue
            with _DECODE_LOCK:
                try:
                    st += obs_read(io.BytesIO(blob), format="MSEED")
                except Exception as e:
                    logger.warning("SDS-lite decode failed %s: %s", path, e)
        return st

    # short alias, mirrors BotListener style (`stream.addStream(...)` then iterate)
    read = get_waveforms

    # ------------------------------------------------------------------
    def _find_files(self, net, sta, loc, chan, t0, t1):
        """Glob candidate day-files covering [t0, t1] ± 1 day (border overlap)."""
        from obspy import UTCDateTime

        loc_pat = loc if loc not in ("", None) else "*"
        days = set()
        t = UTCDateTime(t0.year, t0.month, t0.day) - 86400
        t_max = t1 + 86400
        while t < t_max:
            days.add((t.year, t.julday))
            t += 86400

        paths = []
        for year, doy in sorted(days):
            pattern = (self.sds_root / str(year) / net / sta / f"{chan}.D"
                       / f"{net}.{sta}.{loc_pat}.{chan}.D.{year}.{doy:03d}")
            paths += glob.glob(str(pattern))
        return sorted(set(paths))

    # ------------------------------------------------------------------
    @staticmethod
    def _filter_records(path, t0, t1):
        """Single sequential read, then lightweight scan (mseedlite, no STEIM
        decode, no lock) to collect raw bytes of records overlapping [t0, t1].
        Returns bytes ready for ObsPy decode (may be empty)."""
        t0_dt, t1_dt = t0.datetime, t1.datetime
        chunks = []
        try:
            with open(path, "rb") as fd:
                buf = io.BytesIO(fd.read())          # 1x sequential read, HDD-friendly
        except OSError as e:
            logger.debug("SDS-lite open failed %s: %s", path, e)
            return b""

        try:
            for rec in mseed.Input(buf):
                try:
                    if rec.end_time < t0_dt or rec.begin_time > t1_dt:
                        continue
                except TypeError:
                    continue
                chunks.append(rec.header + rec.data)
        except mseed.MSeedError as e:
            logger.debug("SDS-lite scan stopped early %s: %s", path, e)

        return b"".join(chunks)

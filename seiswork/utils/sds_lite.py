#!/usr/bin/env python3
"""
SeisWork — SDS Lite Reader (lightweight MiniSEED window reader)
Author : HakimBMKG

Pattern learned from BotSeiscompListener/bin/BotListener.py
(`_process_and_repick`): instead of `obspy.read()` on the full file, BotListener
opens `seiscomp.io.RecordStream`, calls `addStream(net,sta,loc,cha,t0,t1)`,
then iterates `RecordInput(..., Record.DATA_ONLY)` RECORD-BY-RECORD —
each record is converted to an array without constructing a full Stream/Trace
for the whole file.  Lightweight because it ONLY touches the requested time range,
through native SeisComP I/O (C++, no Python libmseed global state).

`seiscomp.io` (compiled `_io.so`/`_core.so`) CANNOT be imported in the
`seiswork` env — its compiled ABI differs from the conda libpython (see
mseedlite.py for details).  This module CLONES the approach, not the binary:

  1. `mseedlite` (cloned from seiscomp/lib/python/seiscomp/mseedlite.py,
     pure Python — struct.unpack only, NO STEIM decode, NO global C state
     → thread-safe, no _MSEED_LOCK needed) scans the header of each record
     in the SDS day-file — record_time, size — WITHOUT building a Trace.
  2. Only the raw bytes of records overlapping [t_start, t_end] are collected.
  3. ObsPy decodes ONLY that filtered subset (not the full day) —
     the expensive part (STEIM decompress + Stream/Trace construction) shrinks
     from "one full day" → "exactly what is needed".

I/O note (see project_seiswork memory — io_processes smoke test findings):
Rotational HDD `/dev/sda` is the TRUE bottleneck (seek-thrashing when many
processes do random reads concurrently).  The file is therefore read ONCE
SEQUENTIALLY (`fd.read()` full → BytesIO) — HDD-friendly — then filtered in
memory; NO per-record seek/random-read from disk.

══════════════════════════════════════════════════════════════════════════════
BENCHMARK (notebooks/_test_sds_lite.ipynb, 8 Jun 2026, 7G.SP06..HHZ) — HONEST:
  Correctness : GO ✓ — samples byte-identical to SDSClient (1 day & 1 hour)
  Speed       : NO-GO ✗ for BULK reads (per station-day) —
                1 day : SDSClient 0.34s   vs  lite 1.53s   (4.5× SLOWER)
                1 hour: SDSClient 0.02s   vs  lite 0.85s   (40×  SLOWER)

  Reason: `obspy.clients.filesystem.sds.Client` ALREADY does selective reads
  at the libmseed level (C, compiled) via starttime/endtime kwargs — it does NOT
  decode a full day as originally assumed.  `mseedlite.Input` (pure Python)
  STILL builds a full `Record` object (struct.unpack + STEIM constants) for ALL
  ~70 000 records in a 36 MB day-file, relevant or not — Python loop overhead
  over that many rows is far slower than C.

  Conclusion: the "read record-by-record, touch only what is relevant" pattern
  from BotListener.py IS CORRECT AND LIGHTWEIGHT — but its lightness comes from
  native compiled I/O (`seiscomp.io`/`libseiscomp_io`, C++) AND a small window
  (±20/+40 seconds per repick, not per station-day).  Cloning it in pure Python
  for BULK load (per station-day, thousands of records) is a net-loss.
  → This module is kept (correct, opt-in, `data_source: "sds_lite"`)
  as documentation of the pattern and for SMALL/sparse windows — but NOT
  recommended for bulk production picking; `data_source: "sds"` (SDSClient,
  C-level selective read) remains the validated fastest path.
══════════════════════════════════════════════════════════════════════════════
"""

import io
import glob
import logging
import threading
from pathlib import Path

from . import mseedlite as mseed

logger = logging.getLogger(__name__)

# The final ObsPy/libmseed decode step (only for the filtered subset) uses a
# C-extension that is NOT thread-safe for concurrent disk reads — same spirit
# as _MSEED_LOCK in phasenet.py, but the critical section is MUCH shorter
# (decoding a small subset, not the entire file).
_DECODE_LOCK = threading.Lock()


class SDSLiteReader:
    """Read a time window net.sta.loc.chan from an SDS archive — lightweight.

    Unlike obspy.clients.filesystem.sds.Client.get_waveforms (which decodes
    EVERY record in the day-file into a full Trace, then filters by time),
    this reader scans first (pure Python, no decode) then decodes ONLY the
    relevant bytes.

    Drop-in replacement for `SDSClient(...).get_waveforms(...)` — returns
    an `obspy.Stream`.
    """

    def __init__(self, sds_root):
        self.sds_root = Path(sds_root)

    # ------------------------------------------------------------------
    def get_waveforms(self, net, sta, loc, chan, t_start, t_end):
        """Return an obspy.Stream for [t_start, t_end] — lightweight decode."""
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

    # short alias — mirrors BotListener style (`stream.addStream(...)` then iterate)
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
        """Single sequential read → lightweight scan (mseedlite, no STEIM decode,
        no lock) → collect raw bytes of records overlapping [t0, t1].
        Returns bytes ready for ObsPy decode (may be empty)."""
        t0_dt, t1_dt = t0.datetime, t1.datetime
        chunks = []
        try:
            with open(path, "rb") as fd:
                buf = io.BytesIO(fd.read())          # 1× sequential read — HDD-friendly
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

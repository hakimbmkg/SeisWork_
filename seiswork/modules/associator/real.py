#!/usr/bin/env python3
"""
SeisWork — REAL phase association module
Author : HakimBMKG

Runs the REAL (Rapid Earthquake Association and Location) program.
Converts PhaseNet picks → REAL format, runs REAL, converts output
back to SeisWork standard catalog CSV.

Reference:
  Zhang et al. (2019), Seismol. Res. Lett., doi:10.1785/0220190052
"""

import os
import sys
import shutil
import subprocess
import time
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed

import pandas as pd


CATALOG_COLS = [
    "event_id", "datetime", "lat", "lon", "depth_km",
    "mag", "rms", "nsta", "gap", "method"
]

PICKS_COLS = [
    "event_id", "network", "station", "phase", "pick_time", "prob"
]


def _run_one_day(args: tuple) -> tuple:
    """Run REAL binary for a single day (top-level for ProcessPoolExecutor pickling).

    Args:
        args: (date, cmd, run_dir, log_file)
    Returns:
        (date, run_dir, returncode)
    """
    date, cmd, run_dir, log_file = args
    os.makedirs(run_dir, exist_ok=True)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "w") as logf:
        ret = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT,
                             cwd=run_dir)
    return date, run_dir, ret.returncode


class RealAssociator:
    """REAL phase association wrapper."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = base_dir
        self.rcfg     = cfg["associate"]["real"]
        self.reg      = cfg["region"]

        self.cat_dir  = os.path.join(base_dir, "work", "catalog")
        self.real_dir = os.path.join(base_dir, "work", "real")
        self.log_dir  = os.path.join(base_dir, "work", "logs", "real")
        os.makedirs(self.cat_dir,  exist_ok=True)
        os.makedirs(self.real_dir, exist_ok=True)
        os.makedirs(self.log_dir,  exist_ok=True)

        self.real_exec = self._find_real()

    def _find_real(self) -> str:
        name = self.rcfg.get("exec", "REAL")
        # 1) valid absolute exec → use it directly (e.g. realtime pointing at the
        #    bundled core/bin/REAL).
        if os.path.isabs(name) and os.path.exists(name):
            return name
        # 2) PRIORITIZE the bundled binary in the seiswork source tree (portable — same
        #    principle as VELEST/Hypoinverse). repo root = parents[3] of this file
        #    (seiswork/modules/associator/real.py).
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                  "..", "..", ".."))
        bundled = [
            os.path.join(repo_root,      "core", "bin", "REAL"),
            os.path.join(self.base_dir,  "core", "bin", "REAL"),
            os.path.join(self.base_dir,  "core", "REAL", "bin", "REAL"),
        ]
        for c in bundled:
            if os.path.exists(c):
                return c
        # 3) Fallback: PATH, then the standard install location ~/apps/REAL.
        found = shutil.which(name)
        if not found:
            alt = os.path.join(os.path.expanduser("~"), "apps", "REAL", "bin", "REAL")
            if os.path.exists(alt):
                return alt
        return found or ""

    # ── Convert picks.csv → REAL per-day pick directories ─────────────────────
    # REAL.c reads ONE DAY at a time (`-D year/mon/day/...`) from a directory of
    # per-station files literally named `NET.STA.P.txt` / `NET.STA.S.txt`
    # (sprintf "%s/%s.%s.P.txt", dir, net, sta — REAL.c:355,374), each line
    # `trig weight amp` where trig = seconds since 00:00 UTC of that day
    # (fscanf "%lf %lf %lf" — REAL.c:362).  A single combined phase_allday.txt
    # is not a format REAL ever reads.
    def _picks_to_real_dirs(self, picks_file: str):
        os.makedirs(self.real_dir, exist_ok=True)
        df = pd.read_csv(picks_file)
        df.columns = [c.lower().strip() for c in df.columns]
        df["time"] = pd.to_datetime(df["phase_time"], utc=True, format="mixed")
        df["date"] = df["time"].dt.strftime("%Y%m%d")

        days = []
        for date, day_df in df.groupby("date"):
            day_dir = os.path.join(self.real_dir, date)
            os.makedirs(day_dir, exist_ok=True)
            midnight = pd.Timestamp(f"{date[:4]}-{date[4:6]}-{date[6:]}", tz="UTC")
            n_files = 0
            for (net, sta, phase), grp in day_df.groupby(["network", "station", "phase_hint"]):
                if phase not in ("P", "S"):
                    continue
                out = os.path.join(day_dir, f"{net}.{sta}.{phase}.txt")
                with open(out, "w") as f:
                    for _, r in grp.sort_values("time").iterrows():
                        trig = (r["time"] - midnight).total_seconds()
                        amp  = r["phase_amp"] if pd.notna(r["phase_amp"]) else 0.0
                        f.write(f"{trig:.3f} {r['phase_score']:.3f} {amp:.6e}\n")
                n_files += 1
            print(f"[REAL] {date}: {n_files} pick files (P/S per station) → {day_dir}", flush=True)
            days.append((date, day_dir))
        return sorted(days)

    # ── Convert station file to REAL station format ───────────────────────────
    # REAL.c Readstation(): fscanf("%lf %lf %s %s %s %lf", &stlo, &stla,
    #   net, sta, comp, &elev) — LON FIRST, then LAT, then net/sta/comp/elev_km
    # (REAL.c:1606).  Writing `STA lat lon elev_km` (4 cols, no net/comp, lat
    # before lon) would make REAL misread every field (stlo<-STA string parse
    # failure, stla<-lat, net<-lon, ...).
    def _write_real_stations(self, station_df: pd.DataFrame) -> str:
        os.makedirs(self.real_dir, exist_ok=True)   # ensure dir exists before write
        sta_file = os.path.join(self.real_dir, "stations.txt")
        with open(sta_file, "w") as f:
            for _, r in station_df.iterrows():
                f.write(f"{r['lon']:.4f} {r['lat']:.4f} {r['network']} {r['station']} "
                        f"HHZ {r.get('elev', 0)/1000.0:.3f}\n")
        return sta_file

    # ── Load station file ──────────────────────────────────────────────────────
    def _load_stations(self) -> pd.DataFrame:
        sta_file = self.cfg["data"]["station_file"]
        # Handle absolute vs relative path
        if not os.path.isabs(sta_file):
            sta_file = os.path.join(self.base_dir, sta_file)
        if not os.path.exists(sta_file):
            print(f"[ERROR] Station file not found: {sta_file}", flush=True)
            sys.exit(1)
        # NET|STA|LAT|LON|ELEV (5-col) vs NET|STA|LOC|LAT|LON|ELEV (6-col,
        # notebook-generated files with empty LOC) — a fixed usecols=[0,1,2,3,4]
        # mis-reads the 6-col layout (LOC parsed as LAT -> all-NaN, columns
        # shifted).  _load_station_df auto-detects the offset (see Bug #1,
        # project_seiswork memory) and is already used by velest/hypoinverse
        # converters — reuse it here instead of duplicating the broken parse.
        from seiswork.utils.converter import _load_station_df
        df = _load_station_df(sta_file)
        if df.empty:
            df = pd.read_csv(sta_file, sep=r"\s+", header=None,
                             names=["station","lat","lon","elev"])
        return df

    # ── Build REAL travel-time table argument ─────────────────────────────────
    def _get_ttdb(self) -> str:
        ttdb = self.rcfg.get("tt_db", "")
        if not os.path.isabs(ttdb):
            ttdb = os.path.join(self.base_dir, ttdb)
        return ttdb if os.path.exists(ttdb) else ""

    # ── Parse REAL per-event output (phase_sel.txt, written per run dir) ──────
    # phase_sel.txt is a strict SUPERSET of catalog_sel.txt: each event's
    # summary line uses the IDENTICAL fprintf format as CATALOGSEL (REAL.c:876
    # mirrors :869 byte-for-byte), immediately followed by that event's
    # associated phase picks (REAL.c:884):
    #   "%5s %8s %5s %12.4lf %9.4lf %5.2e %7.4lf %8.4lf %8.4lf"
    #    net  sta  phase abs_pk(trig,s)  traveltime  amp   resid  weight  baz
    # Parsing this ONE file — instead of catalog_sel.txt plus a separate parse
    # of phase_sel.txt — guarantees catalog rows and per-event picks line up
    # 1:1, since both originate from the same CLEAR[i] loop (REAL.c:858-890).
    #
    # otime itself is "%04d %02d %02d %02d:%02d:%06.3f" (REAL.c:625) — i.e. it
    # CONTAINS an embedded space — so header lines are recognised by token
    # count (>=17: idx + 4 datetime tokens + 12 numerics) vs phase-pick lines
    # (exactly 9 tokens), not by a fixed split.
    #
    # event_id is assigned 0-based here (event_offset + idx - 1) so it matches
    # `catalog_df["event_id"]` exactly with NO subsequent renumbering needed —
    # see run() below (idx from REAL is 1-based per day).
    def _parse_real_phase_file(self, phase_file: str, midnight: pd.Timestamp,
                               event_offset: int = 0):
        if not os.path.exists(phase_file):
            return pd.DataFrame(columns=CATALOG_COLS), pd.DataFrame(columns=PICKS_COLS)

        cat_rows, pick_rows = [], []
        cur_eid = None
        with open(phase_file) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 17:
                    try:
                        idx = int(parts[0])
                        year, mon, day, hms = parts[1:5]
                        (atime, std, lat, lon, dep, mag_med, mag_std,
                         pcount, scount, pscount, psboth, gap) = parts[5:17]
                        cur_eid = f"real_{event_offset + idx - 1:06d}"
                        cat_rows.append({
                            "event_id" : cur_eid,
                            "datetime" : f"{year}-{mon}-{day}T{hms}",
                            "lat"      : float(lat),
                            "lon"      : float(lon),
                            "depth_km" : float(dep),
                            "mag"      : float(mag_med),
                            "rms"      : float(std),
                            "nsta"     : int(pscount),
                            "gap"      : float(gap),
                            "method"   : "real",
                        })
                    except Exception:
                        cur_eid = None
                elif len(parts) == 9 and cur_eid is not None:
                    try:
                        net, sta, phase, abs_pk, _tt, _amp, _res, weig, _baz = parts
                        pick_time = midnight + pd.Timedelta(seconds=float(abs_pk))
                        pick_rows.append({
                            "event_id" : cur_eid,
                            "network"  : net,
                            "station"  : sta,
                            "phase"    : phase,
                            "pick_time": pick_time.isoformat(),
                            "prob"     : float(weig),
                        })
                    except Exception:
                        pass
        cat_df  = pd.DataFrame(cat_rows,  columns=CATALOG_COLS)
        pick_df = pd.DataFrame(pick_rows, columns=PICKS_COLS)
        return cat_df, pick_df

    # ── Public entry ──────────────────────────────────────────────────────────
    # REAL.c parses argv POSITIONALLY: exactly -D -R -V -S [-G] then
    # `station pickdir [ttime]` — any extra/raw numeric args (e.g. passing
    # lat_min/lat_max/.../dlat/dlon/... as bare strings) get consumed as
    # `station`/`pickdir`/`ttime` themselves, failing with
    # "Can not open file in ReadFile 0.1" (= dlon).  REAL
    # also processes ONE DAY at a time and only loads the travel-time table
    # when `-G` is given (REAL.c:428 `if (igrid==1) Readttime(...)`) — so
    # `-G` is mandatory for `tt_db` to actually be used, and its
    # trx/trh/tdx/tdh MUST match the grid the table was built with
    # (see notebook cell: ttdb.txt was generated with the REAL demo's
    # taup_tt.py, grid = 1.4°/20km/0.01°/1km).
    def run(self, picks_file: str):
        if not self.real_exec:
            print("[ERROR] REAL binary not found. Set path in config or install REAL.", flush=True)
            print("        https://github.com/Dal-mzhang/REAL", flush=True)
            sys.exit(1)

        print("[REAL] Starting association ...", flush=True)
        t0 = time.time()

        station_df = self._load_stations()
        sta_file   = self._write_real_stations(station_df)
        print(f"[REAL] {len(station_df)} stations → {sta_file}", flush=True)
        days       = self._picks_to_real_dirs(picks_file)
        print(f"[REAL] {len(days)} day(s) to process", flush=True)
        ttdb       = self._get_ttdb()

        sr = self.rcfg.get("search",   {})
        tg = self.rcfg.get("tt_grid",  {})
        ve = self.rcfg.get("velocity", {})
        th = self.rcfg.get("threshold", {})
        lat_center = self.rcfg.get("lat_center", self.reg.get("lat", 0.0))

        # REAL's -R has 4 OPTIONAL trailing subfields we never used to send:
        # .../gap/GCarc0/latref0/lonref0. Without an explicit GCarc0, REAL.c
        # defaults it to 180° then narrows it to `distmax` — the LARGEST
        # inter-station distance in the whole station file (REAL.c:423-425) —
        # which for a nationwide multi-network deployment is thousands of km.
        # GCarc0 directly sets the P/S association time window (REAL.c:542-545:
        # tpmax = tpmin0 + GCarc0*111.19/vp0 + ...), so a network-wide GCarc0
        # lets picks from stations thousands of km apart fall inside the same
        # event window — this is what produced the >1000 km REAL-vs-NLLoc
        # jumps observed live. Always pass an explicit, physically sane
        # GCarc0 (default 3° ≈ 330 km — regional/local monitoring radius)
        # so REAL never falls back to the network's own diameter.
        flag_R = (f"-R{sr.get('rx', 1.0)}/{sr.get('rh', 20.0)}/"
                  f"{sr.get('tdx', 0.1)}/{sr.get('tdh', 2.0)}/{sr.get('tint', 5.0)}/"
                  f"{sr.get('gap', 360.0)}/{sr.get('gcarc0', 3.0)}")
        flag_V = f"-V{ve.get('vp0', 6.2)}/{ve.get('vs0', 3.4)}"
        flag_S = (f"-S{th.get('np0', 4)}/{th.get('ns0', 2)}/{th.get('nps0', 6)}/"
                  f"{th.get('npsboth0', 2)}/{th.get('std0', 0.5)}/{th.get('dtps', 0.1)}/"
                  f"{th.get('nrt', 1.5)}/{th.get('drt', 0.0)}")
        flag_G = None
        if ttdb and tg:
            flag_G = (f"-G{tg.get('trx', 1.4)}/{tg.get('trh', 20.0)}/"
                      f"{tg.get('tdx', 0.01)}/{tg.get('tdh', 1.0)}")

        # Determine worker count: respect config, cap at cpu_count to avoid thrashing
        n_cpu = multiprocessing.cpu_count()
        n_workers = min(int(self.rcfg.get("n_workers", 4)), n_cpu)
        n_workers = max(1, n_workers)
        print(f"[REAL] Running {len(days)} day(s) with {n_workers} parallel worker(s)", flush=True)

        # Build task list — each day is fully independent (separate run_dir)
        tasks = []
        for date, day_dir in days:
            year, mon, day = date[:4], date[4:6], date[6:]
            cmd = [self.real_exec, f"-D{year}/{mon}/{day}/{lat_center}",
                   flag_R, flag_V, flag_S]
            if flag_G:
                cmd.append(flag_G)
            cmd += [sta_file, day_dir]
            if ttdb:
                cmd.append(ttdb)
            run_dir  = os.path.join(self.real_dir, f"run_{date}")
            log_file = os.path.join(self.log_dir,  f"real_{date}.log")
            tasks.append((date, cmd, run_dir, log_file))

        # Run days in parallel; collect run_dirs keyed by date
        run_dirs: dict = {}
        done_count = 0
        with ProcessPoolExecutor(max_workers=n_workers) as ex:
            futs = {ex.submit(_run_one_day, t): t[0] for t in tasks}
            for fut in as_completed(futs):
                date_done, run_dir_done, rc = fut.result()
                done_count += 1
                run_dirs[date_done] = run_dir_done
                status = f"exit {rc}" if rc != 0 else "ok"
                print(f"[REAL] {date_done}: {status}  ({done_count}/{len(tasks)})", flush=True)

        # Parse results in chronological order so event_offset accumulates correctly
        all_events, all_picks = [], []
        for date, *_ in sorted(tasks, key=lambda t: t[0]):
            run_dir  = run_dirs[date]
            midnight = pd.Timestamp(f"{date[:4]}-{date[4:6]}-{date[6:]}", tz="UTC")
            # event_offset must be the running TOTAL event count, not the number
            # of days processed so far — len(all_events) counts DataFrames (one
            # per day), so any day with >1 event collides with the next day's
            # IDs (e.g. day 1 has 2 events -> day 2 starts at real_000001 again,
            # clashing with day 1's real_000001).
            day_cat, day_picks = self._parse_real_phase_file(
                os.path.join(run_dir, "phase_sel.txt"), midnight,
                event_offset=sum(len(df) for df in all_events))
            if not day_cat.empty:
                print(f"[REAL] {date}: {len(day_cat)} events, {len(day_picks)} picks", flush=True)
                all_events.append(day_cat)
                all_picks.append(day_picks)
            else:
                print(f"[REAL] {date}: 0 events", flush=True)

        if not all_events:
            print("[REAL] No events found.", flush=True)
            return

        catalog_df = pd.concat(all_events, ignore_index=True)
        picks_df   = pd.concat(all_picks,  ignore_index=True) if all_picks \
                     else pd.DataFrame(columns=PICKS_COLS)

        out_cat  = os.path.join(self.cat_dir, "catalog_real.csv")
        out_pick = os.path.join(self.cat_dir, "picks_real.csv")
        catalog_df.to_csv(out_cat,  index=False)
        picks_df.to_csv(out_pick, index=False)

        # Canonical names — NLLocLocator._catalog_to_obs (and other downstream
        # converters) look these up regardless of which associator produced
        # them, mirroring the existing catalog_associated.csv convention
        # (see GammaAssociator.run; nlloc.py edited to also accept
        # picks_associated.csv as the primary picks lookup).
        catalog_df.to_csv(os.path.join(self.cat_dir, "catalog_associated.csv"), index=False)
        picks_df.to_csv(os.path.join(self.cat_dir, "picks_associated.csv"), index=False)

        elapsed = time.time() - t0
        print(f"[REAL] Done. {len(catalog_df)} events, {len(picks_df)} picks → {out_cat}  ({elapsed:.1f}s)", flush=True)
        return out_cat

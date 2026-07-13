"""Cross-correlation diagnostics for SeisWork (by HakimBMKG).

Reads FDTCC / hypoDD dt.cc output plus the job waveform SAC cache to produce:
  - CC coefficient statistics (histogram, per-station, per-phase, CC-vs-dist)
  - Top event-pair list with high mean CC
  - Waveform pairs for a chosen (ev1, ev2, sta, pha) from SAC files

dt.cc format (FDTCC / hypoDD):
    # EV1  EV2  DIST(km)
    STA  DT(s)  CC   PHA
    ...
    # ...

event.dat format (hypoDD input, from ph2dt):
    ID LAT LON DEP MAG ? ? ? YYYY/MM/DD HH:MM:SS.ff

hypoDD.pha (phase file):
    # ID LAT LON DEP MAG ? ? ? YYYYMMDD HH:MM:SS.ff
    STA TT WGHT PHA
"""
from __future__ import annotations

import os
import glob
import math
from datetime import datetime, timedelta
from typing import Optional, List, Dict

import numpy as np


# ────────────────────────────────────────────────────────────────────────────
# dt.cc parser
# ────────────────────────────────────────────────────────────────────────────
def parse_dtcc(path: str) -> dict:
    """Parse dt.cc into dict with arrays ev1, ev2, dist_km, sta, dt_s, cc, phase."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    ev1, ev2, dist_km = [], [], []
    sta, dt_s, cc, phase = [], [], [], []

    cur_ev1 = cur_ev2 = cur_dist = None
    with open(path, errors="replace") as fh:
        for ln in fh:
            ln = ln.rstrip()
            if not ln:
                continue
            if ln.startswith("#"):
                parts = ln.split()
                if len(parts) >= 3:
                    try:
                        cur_ev1  = int(parts[1])
                        cur_ev2  = int(parts[2])
                        cur_dist = float(parts[3]) if len(parts) > 3 else 0.0
                    except ValueError:
                        pass
                continue
            if cur_ev1 is None:
                continue
            parts = ln.split()
            if len(parts) < 4:
                continue
            try:
                ev1.append(cur_ev1);  ev2.append(cur_ev2)
                dist_km.append(cur_dist)
                sta.append(parts[0])
                dt_s.append(float(parts[1]))
                cc.append(float(parts[2]))
                phase.append(parts[3].upper())
            except (ValueError, IndexError):
                continue

    return {
        "ev1"    : np.array(ev1,     dtype=int),
        "ev2"    : np.array(ev2,     dtype=int),
        "dist_km": np.array(dist_km, dtype=float),
        "sta"    : np.array(sta,     dtype=object),
        "dt_s"   : np.array(dt_s,   dtype=float),
        "cc"     : np.array(cc,      dtype=float),
        "phase"  : np.array(phase,   dtype=object),
    }


# ────────────────────────────────────────────────────────────────────────────
# event.dat / hypoDD.pha parsers
# ────────────────────────────────────────────────────────────────────────────
def parse_event_dat(path: str) -> dict:
    """Parse event.dat (ph2dt output) into {id: {lat, lon, dep, mag, time_str}}.

    ph2dt event.dat format (space-separated):
      YYYYMMDD  HHMMSSff  LAT  LON  DEP  MAG  EH  EZ  RMS  ID
    Where HHMMSSff is 8-digit packed time (HHMMSSHH, where HH=hundredths).
    """
    events: dict = {}
    if not os.path.exists(path):
        return events
    with open(path, errors="replace") as fh:
        for ln in fh:
            p = ln.split()
            if len(p) < 10:
                continue
            try:
                date_s = p[0]    # YYYYMMDD
                time_s = p[1]    # HHMMSSff  (e.g. 18452447)
                lat    = float(p[2])
                lon    = float(p[3])
                dep    = float(p[4])
                mag    = float(p[5])
                eid    = int(p[-1])
                # parse packed time -> HH:MM:SS.ff
                t = str(time_s).zfill(8)
                hh, mm, ss, ff = t[0:2], t[2:4], t[4:6], t[6:8]
                ts = f"{date_s} {hh}:{mm}:{ss}.{ff}0000"
                events[eid] = {"lat": lat, "lon": lon, "dep": dep, "mag": mag, "ts": ts}
            except (ValueError, IndexError):
                continue
    return events


def parse_hypodd_pha(path: str) -> dict:
    """Parse hypoDD.pha into {eid: {ts_origin(str), picks: {sta: {P: tt, S: tt}}}}.

    Two common formats:
      A)  # YYYY MM DD HH MM SS.ff  LAT LON DEP MAG ? ? ?  EV_ID
          STA  TT  WGHT  PHA
      B)  # EV_ID LAT LON DEP MAG ? ? ?  YYYYMMDD  HH:MM:SS.ff
          STA  TT  WGHT  PHA
    """
    if not os.path.exists(path):
        return {}
    result: dict = {}
    cur_id = None
    with open(path, errors="replace") as fh:
        for ln in fh:
            ln = ln.rstrip()
            if ln.startswith("#"):
                parts = ln.split()
                if len(parts) < 5:
                    continue
                try:
                    p1 = parts[1]
                    # Format A: parts[1] is a 4-digit year (e.g. 2016)
                    if len(p1) == 4 and p1.isdigit() and int(p1) > 1900:
                        yr, mo, dy = int(parts[1]), int(parts[2]), int(parts[3])
                        hr, mn     = int(parts[4]), int(parts[5])
                        sc         = float(parts[6])
                        sec_i = int(sc); ms_i = round((sc - sec_i) * 1000000)
                        ts = datetime(yr, mo, dy, hr, mn, sec_i, ms_i)
                        cur_id = int(parts[-1])  # EV_ID is the last token
                        result[cur_id] = {"ts": ts.strftime("%Y%m%d %H:%M:%S.%f"), "picks": {}}
                    else:
                        # Format B: parts[1] = EV_ID; parts[-2]=YYYYMMDD, parts[-1]=HH:MM:SS.ff
                        cur_id = int(parts[1])
                        date_s = parts[-2]
                        time_s = parts[-1]
                        result[cur_id] = {"ts": f"{date_s} {time_s}", "picks": {}}
                except (ValueError, IndexError):
                    cur_id = None
            elif cur_id is not None and ln.strip():
                parts = ln.split()
                if len(parts) >= 3:
                    try:
                        s  = parts[0]
                        tt = float(parts[1])
                        # column 3 is WGHT, column 4 (index 3) is phase
                        ph = (parts[3] if len(parts) > 3 else parts[2]).upper()
                        result[cur_id]["picks"].setdefault(s, {})[ph] = tt
                    except (ValueError, IndexError):
                        pass
    return result


# ────────────────────────────────────────────────────────────────────────────
# statistics helpers
# ────────────────────────────────────────────────────────────────────────────
def _hist_cc(d: np.ndarray, nbins: int = 30) -> dict:
    d = d[np.isfinite(d)]
    if d.size < 2:
        return {"edges": [], "counts": []}
    edges = np.linspace(0.0, 1.0, nbins + 1)
    counts, _ = np.histogram(d, bins=edges)
    return {"edges": [round(float(e), 4) for e in edges],
            "counts": [int(c) for c in counts]}


def _per_station_cc(data: dict) -> list:
    """Mean CC per station for P and S."""
    stations = sorted(set(data["sta"].tolist()))
    out = []
    for s in stations:
        msk = data["sta"] == s
        row: dict = {"sta": s}
        for ph in ("P", "S"):
            pm = msk & (data["phase"] == ph)
            d  = data["cc"][pm]
            d  = d[np.isfinite(d)]
            if d.size:
                row[ph.lower()] = {"mean": round(float(d.mean()), 4), "n": int(d.size)}
        out.append(row)
    return out


def _scatter_cc_dist(data: dict, ph: str, max_pts: int = 3000) -> dict:
    msk = data["phase"] == ph
    x = data["dist_km"][msk]
    y = data["cc"][msk]
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if x.size > max_pts:
        idx = np.random.default_rng(1).choice(x.size, max_pts, replace=False)
        x, y = x[idx], y[idx]
    return {"x": [round(float(v), 3) for v in x],
            "y": [round(float(v), 4) for v in y]}


def _top_pairs(data: dict, n: int = 20) -> list:
    """Return top-N event pairs sorted by mean CC (all phases)."""
    pairs: Dict[tuple, list] = {}
    for i in range(len(data["ev1"])):
        k = (int(data["ev1"][i]), int(data["ev2"][i]))
        pairs.setdefault(k, []).append(float(data["cc"][i]))
    ranked = sorted(pairs.items(), key=lambda kv: -np.mean(kv[1]))[:n]
    return [{"ev1": k[0], "ev2": k[1],
             "n_obs": len(v),
             "mean_cc": round(float(np.mean(v)), 4),
             "dist_km": round(float(np.mean(
                 data["dist_km"][(data["ev1"] == k[0]) & (data["ev2"] == k[1])]
             )), 3)} for k, v in ranked]


# ────────────────────────────────────────────────────────────────────────────
# SAC waveform reading
# ────────────────────────────────────────────────────────────────────────────
def _read_sac_pair(job_dir: str, ev1: int, ev2: int, sta: str,
                   pha_data: dict, phase: str = "P",
                   win_before: float = 1.0, win_after: float = 2.5,
                   max_samples: int = 2000,
                   sds_root: Optional[str] = None) -> Optional[dict]:
    """Read a waveform pair (ev1, ev2) for *sta* around the *phase* arrival.

    Returns dict with keys:
      t1, y1, t2, y2  : time axis (s relative to ev1 origin) + normalised samples
      dt_s, cc         : from dt.cc (passed in)
      ot1, ot2         : origin-time strings

    Falls back to SDS archive (*sds_root*) when the job's waveforms/ dir is absent.
    """
    try:
        from obspy import read as ob_read, UTCDateTime
    except ImportError:
        return None

    wv_root = os.path.join(job_dir, "waveforms")
    has_sac = os.path.isdir(wv_root)
    has_sds = bool(sds_root) and os.path.isdir(sds_root)
    if not has_sac and not has_sds:
        return None

    info1 = pha_data.get(ev1, {})
    info2 = pha_data.get(ev2, {})
    picks1 = info1.get("picks", {}).get(sta, {})
    picks2 = info2.get("picks", {}).get(sta, {})
    tt1 = picks1.get(phase)
    tt2 = picks2.get(phase)
    if tt1 is None or tt2 is None:
        return None

    def _parse_ot(ts_str: str) -> Optional[object]:
        for fmt in ("%Y%m%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S.%f",
                    "%Y%m%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return UTCDateTime(datetime.strptime(ts_str.strip(), fmt))
            except ValueError:
                pass
        return None

    ot1 = _parse_ot(info1.get("ts", ""))
    ot2 = _parse_ot(info2.get("ts", ""))
    if ot1 is None or ot2 is None:
        return None

    def _normalise_cut(tr, ot):
        rel_start = float(tr.stats.starttime - ot)
        dt_sec    = tr.stats.delta
        n = len(tr.data)
        times = [round(rel_start + i * dt_sec, 4) for i in range(n)]
        data = tr.data.astype(float)
        mx = max(abs(data.max()), abs(data.min()))
        if mx > 0:
            data = data / mx
        if n > max_samples:
            step = math.ceil(n / max_samples)
            data  = data[::step]
            times = times[::step]
        return times, data.tolist()

    def _cut_sac(ot, tt):
        t_pick  = ot + tt
        t_start = t_pick - win_before
        t_end   = t_pick + win_after
        sac_files = sorted(glob.glob(os.path.join(wv_root, "*", f"*.{sta}.HHZ")))
        if not sac_files:
            sac_files = sorted(glob.glob(os.path.join(wv_root, "*", f"*.{sta}.*Z")))
        for sf in sac_files:
            try:
                st = ob_read(sf, starttime=t_start, endtime=t_end)
                if not st:
                    continue
                st.detrend("demean")
                return _normalise_cut(st[0], ot)
            except Exception:
                continue
        return None, None

    def _cut_sds(ot, tt):
        t_pick  = ot + tt
        t_start = t_pick - win_before
        t_end   = t_pick + win_after
        try:
            from obspy.clients.filesystem.sds import Client as SDSClient
            from seiswork.utils.channels import read_best_waveform
            client = SDSClient(sds_root)
            st, _cha = read_best_waveform(client, "*", sta, t_start, t_end)
            if st:
                st.detrend("demean")
                return _normalise_cut(st[0], ot)
        except Exception:
            pass
        return None, None

    _cut = _cut_sac if has_sac else _cut_sds

    t1_ax, y1 = _cut(ot1, tt1)
    t2_ax, y2 = _cut(ot2, tt2)

    if not t1_ax or not t2_ax:
        return None

    return {
        "t1": t1_ax, "y1": y1,
        "t2": t2_ax, "y2": y2,
        "win_before": win_before,
        "win_after" : win_after,
        "phase"     : phase,
        "ot1"       : info1.get("ts", ""),
        "ot2"       : info2.get("ts", ""),
        "tt1"       : round(tt1, 3),
        "tt2"       : round(tt2, 3),
    }


def _parse_dtct(path: str) -> dict:
    """Parse dt.ct (catalog differential times) into same schema as dt.cc dict.

    dt.ct format:
        # EV1  EV2  (header for each pair)
        STA  TT1  TT2  WGHT  PHA
    DT = TT1 − TT2; WGHT used as proxy CC weight.
    """
    if not os.path.exists(path):
        return {}
    ev1, ev2, dist_km = [], [], []
    sta, dt_s, cc, phase = [], [], [], []
    cur_ev1 = cur_ev2 = None
    with open(path, errors="replace") as fh:
        for ln in fh:
            ln = ln.rstrip()
            if not ln:
                continue
            if ln.startswith("#"):
                parts = ln.split()
                if len(parts) >= 3:
                    try:
                        cur_ev1 = int(parts[1])
                        cur_ev2 = int(parts[2])
                    except ValueError:
                        pass
                continue
            if cur_ev1 is None:
                continue
            parts = ln.split()
            if len(parts) < 4:
                continue
            try:
                tt1 = float(parts[1]); tt2 = float(parts[2])
                wt  = float(parts[3])
                ph  = parts[4].upper() if len(parts) > 4 else "P"
                ev1.append(cur_ev1);  ev2.append(cur_ev2)
                dist_km.append(0.0)   # dist not in dt.ct
                sta.append(parts[0])
                dt_s.append(tt1 - tt2)
                cc.append(wt)         # weight as proxy CC
                phase.append(ph)
            except (ValueError, IndexError):
                continue
    if not ev1:
        return {}
    return {
        "ev1"    : np.array(ev1,     dtype=int),
        "ev2"    : np.array(ev2,     dtype=int),
        "dist_km": np.array(dist_km, dtype=float),
        "sta"    : np.array(sta,     dtype=object),
        "dt_s"   : np.array(dt_s,   dtype=float),
        "cc"     : np.array(cc,      dtype=float),
        "phase"  : np.array(phase,   dtype=object),
        "_source": "dt.ct",
    }


# ────────────────────────────────────────────────────────────────────────────
# public API
# ────────────────────────────────────────────────────────────────────────────
def crosscorr_report(job_dir: str) -> dict:
    """Build the full CC diagnostics report for one relocation job directory.

    Uses dt.cc (FDTCC real CC) if available; falls back to dt.ct (catalog
    differential times, weight used as CC proxy) when only catalog mode ran
    but SAC waveforms are still present.
    """
    # Locate dt.cc first (FDTCC), then dt.ct (catalog mode)
    dtcc_path = ""
    source_label = "dt.cc (FDTCC)"
    for candidate in [
        os.path.join(job_dir, "dt.cc"),
        os.path.join(job_dir, "IN", "dt.cc"),
    ]:
        if os.path.exists(candidate):
            dtcc_path = candidate
            break

    if not dtcc_path:
        # fallback: dt.ct (catalog pairs + waveforms may still exist)
        dtct_path = os.path.join(job_dir, "dt.ct")
        if not os.path.exists(dtct_path):
            return {"ok": False,
                    "error": "dt.cc / dt.ct not found — run relocation first"}
        try:
            data = _parse_dtct(dtct_path)
        except Exception as e:
            return {"ok": False, "error": f"Failed to read dt.ct: {e}"}
        if not data:
            return {"ok": False, "error": "dt.ct is empty or has no event pairs"}
        dtcc_path = dtct_path
        source_label = "dt.ct (catalog DT, weight as CC proxy)"
    else:
        try:
            data = parse_dtcc(dtcc_path)
        except Exception as e:
            return {"ok": False, "error": f"Failed to read dt.cc: {e}"}

    if data["cc"].size == 0:
        return {"ok": False, "error": "No cross-correlation observations available"}

    # separate P / S
    cc_all = data["cc"]
    cc_p   = data["cc"][data["phase"] == "P"]
    cc_s   = data["cc"][data["phase"] == "S"]

    pairs_set = set(zip(data["ev1"].tolist(), data["ev2"].tolist()))
    evs_set   = set(data["ev1"].tolist()) | set(data["ev2"].tolist())

    def _cc_stats(arr: np.ndarray, label: str) -> dict:
        arr = arr[np.isfinite(arr)]
        if arr.size == 0:
            return {"fase": label, "n": 0}
        return {
            "fase"  : label,
            "n"     : int(arr.size),
            "mean"  : round(float(arr.mean()), 4),
            "median": round(float(np.median(arr)), 4),
            "std"   : round(float(arr.std()), 4),
            "min"   : round(float(arr.min()), 4),
            "max"   : round(float(arr.max()), 4),
            "pct_good": round(float(np.mean(arr >= 0.7) * 100.0), 2),
        }

    report = {
        "ok"        : True,
        "dtcc_path" : dtcc_path,
        "source"    : source_label,
        "summary" : {
            "n_obs"   : int(cc_all.size),
            "n_p"     : int(cc_p.size),
            "n_s"     : int(cc_s.size),
            "n_pairs" : len(pairs_set),
            "n_events": len(evs_set),
            "n_sta"   : int(len(set(data["sta"].tolist()))),
            "mean_cc" : round(float(cc_all.mean()), 4) if cc_all.size else None,
            "mean_cc_p": round(float(cc_p.mean()), 4) if cc_p.size else None,
            "mean_cc_s": round(float(cc_s.mean()), 4) if cc_s.size else None,
        },
        "stats"   : [_cc_stats(cc_all, "Semua"),
                     _cc_stats(cc_p,   "P"),
                     _cc_stats(cc_s,   "S")],
        "hist"    : {
            "all": _hist_cc(cc_all),
            "p"  : _hist_cc(cc_p),
            "s"  : _hist_cc(cc_s),
        },
        "per_station" : _per_station_cc(data),
        "scatter_p"   : _scatter_cc_dist(data, "P"),
        "scatter_s"   : _scatter_cc_dist(data, "S"),
        "top_pairs"   : _top_pairs(data, n=30),
        "has_waveforms": os.path.isdir(os.path.join(job_dir, "waveforms")),
    }
    return report


def crosscorr_waveforms(job_dir: str, ev1: int, ev2: int,
                         sta: str, phase: str = "P",
                         sds_root: Optional[str] = None) -> dict:
    """Return waveform pair (ev1, ev2) at *sta* for the given *phase*.

    Also include DT and CC from dt.cc for annotation.
    Reads SAC files from job_dir/waveforms/ when available; falls back to
    the SDS archive at *sds_root* for catalog-mode jobs without SAC cache.
    """
    # Locate dt.cc or dt.ct for DT/CC values
    dt_val = cc_val = None
    for c in [os.path.join(job_dir, "dt.cc"), os.path.join(job_dir, "IN", "dt.cc")]:
        if os.path.exists(c):
            try:
                d = parse_dtcc(c)
                msk = ((d["ev1"] == ev1) & (d["ev2"] == ev2) &
                       (d["sta"] == sta) & (d["phase"] == phase.upper()))
                if msk.any():
                    dt_val = round(float(d["dt_s"][msk][0]), 4)
                    cc_val = round(float(d["cc"][msk][0]), 4)
            except Exception:
                pass
            break
    if dt_val is None:
        dtct_c = os.path.join(job_dir, "dt.ct")
        if os.path.exists(dtct_c):
            try:
                d = _parse_dtct(dtct_c)
                if d:
                    msk = ((d["ev1"] == ev1) & (d["ev2"] == ev2) &
                           (d["sta"] == sta) & (d["phase"] == phase.upper()))
                    if msk.any():
                        dt_val = round(float(d["dt_s"][msk][0]), 4)
                        cc_val = round(float(d["cc"][msk][0]), 4)
            except Exception:
                pass

    # Parse phase file for arrival times (try multiple naming conventions)
    pha_path = ""
    for _pha_candidate in ["hypoDD.pha", "hypodd_phase.pha", "hypoDD_phase.pha"]:
        _c = os.path.join(job_dir, _pha_candidate)
        if os.path.exists(_c):
            pha_path = _c
            break
    pha_data = parse_hypodd_pha(pha_path) if pha_path else {}
    if not pha_data:
        return {"ok": False, "error": "hypoDD.pha not found — cannot determine arrival times"}

    pair_data = _read_sac_pair(job_dir, ev1, ev2, sta, pha_data,
                               phase=phase.upper(), sds_root=sds_root)
    if not pair_data:
        src = "SDS archive" if sds_root else "waveforms/"
        return {"ok": False, "error": (
            f"Waveform not found for EV{ev1}–EV{ev2} sta {sta} phase {phase} "
            f"({src}). Ensure data is available in the SDS archive."
        )}

    pair_data.update({"ok": True, "ev1": ev1, "ev2": ev2, "sta": sta,
                      "dt_s": dt_val, "cc": cc_val})
    return pair_data


def list_stations_for_pair(job_dir: str, ev1: int, ev2: int) -> list:
    """Return list of (sta, phase, cc/weight) for (ev1, ev2) from dt.cc or dt.ct."""
    def _from_data(d: dict) -> list:
        msk = (d["ev1"] == ev1) & (d["ev2"] == ev2)
        if not msk.any():
            return []
        idxs = np.where(msk)[0]
        return sorted({
            (str(d["sta"][i]), str(d["phase"][i]), round(float(d["cc"][i]), 4))
            for i in idxs
        }, key=lambda x: -x[2])

    for c in [os.path.join(job_dir, "dt.cc"), os.path.join(job_dir, "IN", "dt.cc")]:
        if os.path.exists(c):
            try:
                return _from_data(parse_dtcc(c))
            except Exception:
                return []
    dtct_c = os.path.join(job_dir, "dt.ct")
    if os.path.exists(dtct_c):
        try:
            d = _parse_dtct(dtct_c)
            return _from_data(d) if d else []
        except Exception:
            pass
    return []

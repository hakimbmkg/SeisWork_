#!/usr/bin/env python3
"""
SeisWork — Format converter utilities
Author : HakimBMKG

Central hub for all inter-module format conversions:
  picks.csv  → VELEST .pha
  picks.csv  → NLLoc OBS
  picks.csv  → HypoDD phase.dat
  picks.csv  → SCML XML
  picks.csv  → phaseSA_allday (REAL/SeismAss format)
  catalog    → Hypoinverse ARC
  stations   → VELEST .sta
  stations   → HypoDD station.dat
"""

import os
import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd


# ── Station file converters ───────────────────────────────────────────────────

def _load_station_df(sta_file: str) -> pd.DataFrame:
    """Load any supported station format (pipe-delimited or space-delimited).

    Supported pipe layouts (FDSN/SeisComP "key list" conventions differ in
    whether a location-code column sits between STA and LAT):
      NET|STA|LAT|LON|ELEV[|desc...]        (5-col, e.g. config station metadata)
      NET|STA|LOC|LAT|LON|ELEV[|desc...]    (6-col, e.g. notebook-generated
                                              station files with empty LOC)
    The layout is auto-detected per file by checking which column offset
    yields values inside valid latitude/longitude ranges — a fixed offset
    silently mis-reads one of the two layouts (LOC parsed as LAT → all-NaN).

    Also supported:
      STA LAT LON ELEV                       (space-delimited)
    """
    if not os.path.exists(sta_file):
        return pd.DataFrame()

    # ── Try pipe-delimited (FDSN/SeisComP format) ─────────────────────────────
    try:
        raw = pd.read_csv(sta_file, sep="|", header=None, dtype=str)
        for lat_idx in (2, 3):
            if raw.shape[1] < lat_idx + 3:
                continue
            lat = pd.to_numeric(raw.iloc[:, lat_idx], errors="coerce")
            lon = pd.to_numeric(raw.iloc[:, lat_idx + 1], errors="coerce")
            if not (lat.between(-90, 90) & lon.between(-180, 180)).any():
                continue
            df = raw.iloc[:, [0, 1, lat_idx, lat_idx + 1, lat_idx + 2]].copy()
            df.columns = ["network", "station", "lat", "lon", "elev"]
            df["lat"]  = lat
            df["lon"]  = lon
            df["elev"] = pd.to_numeric(df["elev"], errors="coerce").fillna(0)
            df["station"] = df["station"].str.strip()
            df["network"] = df["network"].str.strip()
            result = df.dropna(subset=["lat", "lon"])
            if not result.empty:
                return result
    except Exception:
        pass

    # ── Fallback: space-delimited (STA LAT LON ELEV) ──────────────────────────
    try:
        df = pd.read_csv(sta_file, sep=r"\s+", header=None,
                         names=["station", "lat", "lon", "elev"])
        df["network"] = "XX"
        df["lat"]  = pd.to_numeric(df["lat"],  errors="coerce")
        df["lon"]  = pd.to_numeric(df["lon"],  errors="coerce")
        df["elev"] = pd.to_numeric(df["elev"], errors="coerce").fillna(0)
        df["station"] = df["station"].str.strip()
        return df.dropna(subset=["lat", "lon"])
    except Exception:
        return pd.DataFrame()


def stations_to_velest_sta(sta_file: str, out_file: str):
    """Convert station file to VELEST .sta format."""
    df = _load_station_df(sta_file)
    with open(out_file, "w") as f:
        f.write("(format: sta_name lat_deg lat_min lon_deg lon_min elev delay_p delay_s)\n")
        for _, r in df.iterrows():
            lat = abs(r["lat"])
            lat_d, lat_m = int(lat), (lat - int(lat)) * 60.0
            lat_ns = "N" if r["lat"] >= 0 else "S"
            lon = abs(r["lon"])
            lon_d, lon_m = int(lon), (lon - int(lon)) * 60.0
            lon_ew = "E" if r["lon"] >= 0 else "W"
            elev_km = r["elev"] / 1000.0
            f.write(f"{r['station']:<6} {lat_d:3d}{lat_m:6.3f}{lat_ns} "
                    f"{lon_d:4d}{lon_m:6.3f}{lon_ew} "
                    f"{elev_km:5.3f}   0.00   0.00\n")


def stations_to_hypodd_fmt(sta_file: str, out_file: str):
    """Convert station file to HypoDD station.dat format."""
    df = _load_station_df(sta_file)
    with open(out_file, "w") as f:
        for _, r in df.iterrows():
            f.write(f"{r['station']:<8}  {r['lat']:9.4f}  {r['lon']:10.4f}  "
                    f"{r['elev']/1000.0:6.3f}\n")


# ── Picks → phase formats ─────────────────────────────────────────────────────

def _normalize_picks(picks_df: pd.DataFrame) -> pd.DataFrame:
    """Ensure consistent picks column names."""
    df = picks_df.copy()
    df.columns = [c.lower().strip() for c in df.columns]
    renames = {
        "phase_hint": "phase", "phase_time": "pick_time",
        "phase_score": "prob", "type": "phase",
        "timestamp": "pick_time",
    }
    for old, new in renames.items():
        if old in df.columns and new not in df.columns:
            df = df.rename(columns={old: new})
    if "phase" in df.columns:
        df["phase"] = df["phase"].str.upper().str[0]
    return df


def picks_to_velest_pha(picks_df: pd.DataFrame, events_df: pd.DataFrame,
                         out_file: str):
    """Write VELEST .pha phase file from picks + events."""
    picks = _normalize_picks(picks_df)
    with open(out_file, "w") as f:
        for _, ev in events_df.iterrows():
            eid = str(ev["event_id"])
            try:
                t = pd.Timestamp(ev["datetime"])
            except Exception:
                continue
            dep   = float(ev.get("depth_km", 10.0))
            mag   = float(ev.get("mag", 0.0))
            if np.isnan(mag):
                mag = 0.0

            lat = abs(float(ev["lat"]))
            lat_d = int(lat); lat_m = (lat - lat_d) * 60.0
            lat_ns = "N" if float(ev["lat"]) >= 0 else "S"
            lon = abs(float(ev["lon"]))
            lon_d = int(lon); lon_m = (lon - lon_d) * 60.0
            lon_ew = "E" if float(ev["lon"]) >= 0 else "W"

            sec = t.second + t.microsecond / 1e6
            f.write(f" {t.year:4d}{t.month:02d}{t.day:02d} {t.hour:02d}{t.minute:02d}"
                    f" {sec:5.2f}  {lat_d:3d}{lat_m:5.2f}{lat_ns} "
                    f"{lon_d:4d}{lon_m:5.2f}{lon_ew}  {dep:6.2f}  {mag:4.1f}  0  0  "
                    f"0.00  {eid}\n")

            # Phase lines
            ev_picks = picks[picks.get("event_index", picks.get("event_id", pd.Series(dtype=str))).astype(str) == eid]
            for _, pk in ev_picks.iterrows():
                try:
                    pt = pd.Timestamp(pk["pick_time"])
                    delay = (pt - t).total_seconds()
                    phase = str(pk["phase"])[0].upper()
                    sta   = str(pk.get("station","????"))[:4]
                    f.write(f"  {sta:<4}  {phase}  0.0  {delay:7.4f}\n")
                except Exception:
                    pass
            f.write("\n")


def catalog_picks_to_nlloc_obs(cat_df: pd.DataFrame, picks_df: pd.DataFrame,
                                out_file: str):
    """Write NLLoc OBS (NLLOC_OBS) phase file."""
    picks = _normalize_picks(picks_df) if not picks_df.empty else pd.DataFrame()
    with open(out_file, "w") as f:
        for _, ev in cat_df.iterrows():
            eid = str(ev["event_id"])
            f.write(f"# {eid}\n")
            if picks.empty:
                continue
            ev_picks = picks[picks.get("event_index", picks.get("event_id", pd.Series(dtype=str))).astype(str) == eid]
            for _, pk in ev_picks.iterrows():
                try:
                    pt  = pd.Timestamp(pk["pick_time"])
                    sta = str(pk.get("station","????"))
                    chn = str(pk.get("channel","HHZ"))
                    net = str(pk.get("network","??"))
                    ph  = str(pk.get("phase","P"))[0].upper()
                    prob = float(pk.get("prob", pk.get("phase_score", 1.0)))
                    err  = max(0.01, (1 - prob) * 0.5)
                    f.write(f"{sta:<6} ?  ?  ?  {ph}      ?    "
                            f"{pt.year:4d}{pt.month:02d}{pt.day:02d}  "
                            f"{pt.hour:02d}{pt.minute:02d} "
                            f"{pt.second + pt.microsecond/1e6:7.4f}  "
                            f"GAU  {err:.4f}  -1.000  -1.000  -1.000\n")
                except Exception:
                    pass


def _pick_event_key(picks: pd.DataFrame) -> str:
    """Pick the picks column that links a pick to its event.

    Different upstream stages tag picks with different keys (GaMMA →
    event_index, REAL/association → event_id, some exports → id). Return the
    first present column so the phase builder joins on the right one instead of
    silently linking nothing.
    """
    for c in ("event_index", "event_id", "event", "evid", "id"):
        if c in picks.columns:
            return c
    return ""


def catalog_picks_to_hypodd_phase(cat_df: pd.DataFrame, picks_df: pd.DataFrame,
                                   out_file: str):
    """Write HypoDD phase.dat file (event headers + station phase rows).

    Picks are joined to events by a shared event key. When the located catalog
    carries an id that no pick references (e.g. the locator re-ids events and
    drops the association id), the id-join yields nothing and ph2dt would later
    report 0 differential-time pairs. To keep relocation-from-picks working we
    (1) try every plausible key and (2) fall back to an origin-time window join
    that assigns each pick to the catalog event whose origin time is closest
    (within a tolerance), mirroring how the LOC-FLOW alur keeps phases attached
    to events through the chain. A loud warning is printed if nothing links.
    """
    picks = _normalize_picks(picks_df) if not picks_df.empty else pd.DataFrame()
    key   = _pick_event_key(picks) if not picks.empty else ""

    # Build a lookup from event key value → catalog event ordinal, and detect
    # whether the id-join will work at all (any overlap between the two id sets).
    cat_ids = {str(ev["event_id"]) for _, ev in cat_df.iterrows()}
    id_join_ok = bool(key) and bool(cat_ids & set(picks[key].astype(str))) if key else False

    # Prepare the time-window fallback: sort catalog origin times once.
    use_time_join = (not picks.empty) and (not id_join_ok)
    if use_time_join:
        ev_times = []
        for i, (_, ev) in enumerate(cat_df.iterrows(), 1):
            try:
                ev_times.append((pd.Timestamp(ev["datetime"]).value, i))
            except Exception:
                pass
        ev_times.sort()
        ev_t_arr = np.array([v for v, _ in ev_times])
        ev_i_arr = np.array([i for _, i in ev_times])
        # Assign each pick to the nearest event by origin time (picks fall after
        # the origin; tolerate up to 90 s P/S moveout at local distances).
        TOL_NS = int(90e9)
        picks = picks.copy()
        pt_ns = pd.to_datetime(picks["pick_time"], errors="coerce", utc=True)
        picks = picks[pt_ns.notna()]
        pt_ns = pt_ns[pt_ns.notna()].astype("int64").to_numpy()
        idx = np.searchsorted(ev_t_arr, pt_ns)
        assigned = np.zeros(len(pt_ns), dtype=int)
        for j, p in enumerate(pt_ns):
            best_i, best_d = 0, TOL_NS + 1
            for k in (idx[j] - 1, idx[j]):
                if 0 <= k < len(ev_t_arr):
                    d = abs(p - ev_t_arr[k])
                    if d < best_d:
                        best_d, best_i = d, ev_i_arr[k]
            assigned[j] = best_i if best_d <= TOL_NS else 0
        picks = picks.assign(_ev_ord=assigned)

    n_pick_rows = 0
    with open(out_file, "w") as f:
        for i, (_, ev) in enumerate(cat_df.iterrows(), 1):
            eid = str(ev["event_id"])
            try:
                t = pd.Timestamp(ev["datetime"])
            except Exception:
                continue
            dep  = float(ev.get("depth_km", 10.0))
            mag  = float(ev.get("mag", 0.0))
            if np.isnan(mag):
                mag = 0.0

            sec = t.second + t.microsecond / 1e6
            f.write(f"# {t.year:4d} {t.month:02d} {t.day:02d} "
                    f"{t.hour:02d} {t.minute:02d} {sec:5.2f} "
                    f"{float(ev['lat']):8.4f} {float(ev['lon']):9.4f} "
                    f"{dep:6.2f} {mag:4.1f} 0.00 0.00 0.00 {i:9d}\n")

            if picks.empty:
                continue
            if use_time_join:
                ev_picks = picks[picks["_ev_ord"] == i]
            elif key:
                ev_picks = picks[picks[key].astype(str) == eid]
            else:
                ev_picks = picks.iloc[0:0]
            ot = t
            for _, pk in ev_picks.iterrows():
                try:
                    pt = pd.Timestamp(pk["pick_time"])
                    if pt.tzinfo is not None:
                        pt = pt.tz_localize(None)
                    ot0 = ot.tz_localize(None) if ot.tzinfo is not None else ot
                    tt = (pt - ot0).total_seconds()
                    sta = str(pk.get("station","????"))[:6]
                    ph  = str(pk.get("phase","P"))[0].upper()
                    wt  = float(pk.get("prob", pk.get("phase_score", 1.0)))
                    f.write(f"{sta:<6}   {tt:7.4f}  {wt:.4f}   {ph}\n")
                    n_pick_rows += 1
                except Exception:
                    pass

    join_kind = "time-window" if use_time_join else (f"id[{key}]" if key else "none")
    if n_pick_rows == 0 and not picks.empty:
        print(f"[WARNING] catalog_picks_to_hypodd_phase: 0 phase rows linked "
              f"(join={join_kind}). Pick event-ids do not match catalog event-ids "
              f"and no pick fell within the time tolerance — relocation will find "
              f"no differential times. Check that picks and catalog come from the "
              f"same association run.")
    else:
        print(f"[phase.dat] {n_pick_rows} phase rows linked via {join_kind} join.")


def catalog_to_hypoinverse_arc(cat_df: pd.DataFrame,
                                picks_df, out_file: str):
    """Write Hypoinverse ARC phase file."""
    picks = _normalize_picks(picks_df) if picks_df is not None and not picks_df.empty else pd.DataFrame()
    with open(out_file, "w") as f:
        for _, ev in cat_df.iterrows():
            eid = str(ev["event_id"])
            try:
                t = pd.Timestamp(ev["datetime"])
            except Exception:
                continue
            lat = abs(float(ev["lat"]))
            lat_d = int(lat); lat_m = int((lat - lat_d) * 6000)
            lat_ns = " " if float(ev["lat"]) >= 0 else "S"
            lon = abs(float(ev["lon"]))
            lon_d = int(lon); lon_m = int((lon - lon_d) * 6000)
            lon_ew = " " if float(ev["lon"]) >= 0 else "W"
            dep  = float(ev.get("depth_km", 10.0))
            sec  = t.second + t.microsecond / 1e6
            f.write(f"{t.year:4d}{t.month:02d}{t.day:02d}{t.hour:02d}{t.minute:02d}"
                    f"{int(sec*100):4d}"
                    f"{lat_d:3d}{lat_ns}{lat_m:4d}"
                    f"{lon_d:4d}{lon_ew}{lon_m:4d}"
                    f"{int(dep*100):5d}\n")

            if picks.empty:
                continue
            ev_picks = picks[picks.get("event_index", picks.get("event_id", pd.Series(dtype=str))).astype(str) == eid]
            for _, pk in ev_picks.iterrows():
                try:
                    pt  = pd.Timestamp(pk["pick_time"])
                    sta = str(pk.get("station","????"))[:4]
                    ph  = str(pk.get("phase","P"))[0].upper()
                    f.write(f"{sta:<4}  {ph}0{int((pt.second + pt.microsecond/1e6)*100):4d}\n")
                except Exception:
                    pass
        f.write("                 10\n")  # terminator


def catalog_picks_to_phaseSA(cat_df: pd.DataFrame, picks_df: pd.DataFrame,
                              out_file: str) -> str:
    """Write a phaseSA_allday-format phase file (REAL / SeismAss convention).

    Output format (identical to phaseSA_allday.txt produced by REAL):

      # YYYY MM DD HH MM SS.sss    LAT      LON     DEPTH  MAG    0.00    0.00    0.00      EVTID
       STA    TT.TTT  W  Phase

    where TT.TTT = pick_time − origin_time (travel time in seconds).

    Handles picks from any SeisWork associator:
      - GaMMA  : columns type/timestamp/event_index  (event_index −1 = unassociated, skipped)
      - REAL   : columns phase/pick_time/event_id
      - raw PhaseNet : columns phase_hint/phase_time  (no event_id — writes no phases)

    Parameters
    ----------
    cat_df   : catalog DataFrame — event_id, datetime, lat, lon, depth_km, mag
    picks_df : associated picks (any associator — normalized internally)
    out_file : output path

    Returns
    -------
    str : path to the written file
    """
    picks = _normalize_picks(picks_df) if not picks_df.empty else pd.DataFrame()

    # Drop GaMMA unassociated picks (event_index == -1) before any matching
    if not picks.empty and "event_index" in picks.columns:
        picks = picks[pd.to_numeric(picks["event_index"], errors="coerce").fillna(-1) >= 0].copy()

    # Pre-parse pick times once for performance
    if not picks.empty and "pick_time" in picks.columns:
        picks["_pt"] = pd.to_datetime(picks["pick_time"], utc=True, format="mixed",
                                      errors="coerce")

    n_written = 0
    with open(out_file, "w") as fout:
        for seq, (_, ev) in enumerate(cat_df.iterrows(), start=1):
            eid = str(ev["event_id"])

            # Parse origin time — accept ISO strings or Timestamp
            try:
                orig_ts = pd.Timestamp(str(ev["datetime"])).tz_localize("UTC") \
                          if pd.Timestamp(str(ev["datetime"])).tzinfo is None \
                          else pd.Timestamp(str(ev["datetime"]))
            except Exception:
                continue

            mag = ev.get("mag", float("nan"))
            try:
                mag = 0.0 if (mag is None or np.isnan(float(mag))) else float(mag)
            except (TypeError, ValueError):
                mag = 0.0

            lat   = float(ev["lat"])
            lon   = float(ev["lon"])
            depth = float(ev["depth_km"])

            # Match picks to this event (works for GaMMA event_index and REAL event_id)
            if picks.empty:
                ev_picks = pd.DataFrame()
            else:
                key_col = picks.get("event_index",
                                    picks.get("event_id", pd.Series(dtype=str)))
                ev_picks = picks[key_col.astype(str) == eid]

            if ev_picks.empty:
                continue   # skip events with no associated picks

            # Header line
            yr, mo, dy = orig_ts.year, orig_ts.month, orig_ts.day
            hr, mi     = orig_ts.hour, orig_ts.minute
            ss         = orig_ts.second + orig_ts.microsecond / 1e6
            fout.write(
                f"# {yr:4d} {mo:02d} {dy:02d} {hr:02d} {mi:02d} {ss:06.3f}"
                f"    {lat:6.4f}   {lon:8.4f}  {depth:6.3f} {mag:5.2f}"
                f"    0.00    0.00    0.00      {seq:06d}\n"
            )

            # Phase lines — sorted by travel time
            phase_rows = []
            orig_epoch = orig_ts.timestamp()
            for _, pk in ev_picks.iterrows():
                try:
                    pt = pk.get("_pt") if "_pt" in pk.index else None
                    if pt is None or pd.isna(pt):
                        pt = pd.Timestamp(str(pk["pick_time"]), tz="UTC")
                    tt = pt.timestamp() - orig_epoch
                    if not (0 <= tt <= 200):   # sanity: travel time 0–200 s
                        continue
                    phase = str(pk.get("phase", pk.get("type", "P")))[0].upper()
                    if phase not in ("P", "S"):
                        continue
                    sta = str(pk.get("station", "????"))
                    phase_rows.append((tt, sta, phase))
                except Exception:
                    continue

            phase_rows.sort()
            for tt, sta, ph in phase_rows:
                fout.write(f" {sta:<4}   {tt:5.3f} 1 {ph}\n")

            n_written += 1

    return str(out_file)


def catalog_picks_to_scml(cat_df: pd.DataFrame, picks_df: pd.DataFrame,
                           out_file: str):
    """Write SCML XML for SeisComP screloc input.

    Format: picks + origins with arrivals referencing those picks.
    screloc --ep reads this and re-locates each origin using LocSAT.
    Depth in SCML is meters (multiply km by 1000).
    """
    picks = _normalize_picks(picks_df) if not picks_df.empty else pd.DataFrame()
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<seiscomp xmlns="http://geofon.gfz-potsdam.de/ns/seiscomp3-schema/0.12" version="0.12">',
             '<EventParameters>']

    pick_blocks  = []
    origin_blocks = []

    for _, ev in cat_df.iterrows():
        eid = str(ev["event_id"])
        ev_picks = picks[picks.get("event_index", picks.get("event_id", pd.Series(dtype=str))).astype(str) == eid]

        arrival_lines = []
        for _, pk in ev_picks.iterrows():
            try:
                pt  = pd.Timestamp(pk["pick_time"])
                sta = str(pk.get("station", "????"))
                net = str(pk.get("network", "??"))
                chn = str(pk.get("channel", "HHZ"))
                loc = str(pk.get("location", ""))
                ph  = str(pk.get("phase", "P"))[0].upper()
                pid = f"SeisWork/{eid}/{sta}/{ph}"
                pick_blocks += [
                    f'  <pick publicID="{pid}">',
                    f'    <time><value>{pt.isoformat()}</value></time>',
                    f'    <waveformID networkCode="{net}" stationCode="{sta}" '
                    f'locationCode="{loc}" channelCode="{chn}"/>',
                    f'    <phaseHint>{ph}</phaseHint>',
                    f'  </pick>',
                ]
                arrival_lines += [
                    f'      <arrival>',
                    f'        <pickID>{pid}</pickID>',
                    f'        <phase>{ph}</phase>',
                    f'      </arrival>',
                ]
            except Exception:
                pass

        try:
            ot    = pd.Timestamp(ev["datetime"])
            lat   = float(ev["lat"])
            lon   = float(ev["lon"])
            dep_m = float(ev.get("depth_km", 10.0)) * 1000.0
            oid   = f"SeisWork/origin/{eid}"
            origin_blocks += [
                f'  <origin publicID="{oid}">',
                f'    <time><value>{ot.isoformat()}</value></time>',
                f'    <latitude><value>{lat}</value></latitude>',
                f'    <longitude><value>{lon}</value></longitude>',
                f'    <depth><value>{dep_m}</value></depth>',
            ] + arrival_lines + ['  </origin>']
        except Exception:
            pass

    lines += pick_blocks + origin_blocks
    lines += ['</EventParameters>', '</seiscomp>']
    with open(out_file, "w") as f:
        f.write("\n".join(lines))

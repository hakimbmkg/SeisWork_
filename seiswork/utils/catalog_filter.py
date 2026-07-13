"""
SeisWork - Catalog filter (by HakimBMKG)

Adapts `simulflow.visualization.MapPlot.filter_pha` to work directly on
SeisWork CSV catalogs (columns: event_id, datetime, lat, lon, depth_km,
mag, rms, nsta, gap, method), keeping the same output format so downstream
pipeline functions can use it.

Criteria: lat/lon/depth bounds, magnitude (min/max), max RMS, min phase
count, max azimuthal gap (degrees), and time range (start/end).

`min_phase` uses the per-event pick count (picks_associated.csv), falling
back to the `nsta` column if picks are unavailable. `max_gap` uses the
catalog's `gap` column if present; otherwise it's computed from station
azimuths (needs station coordinates), same as simulflow's filter_pha.
"""
import math
import warnings

import numpy as np
import pandas as pd


# ── Azimuth & gap (ported from simulflow MapPlot) ────────────────────────────
def _calc_azimuth(ev_lat, ev_lon, sta_lat, sta_lon):
    """Azimuth (0-360 deg) from event to station."""
    lat1, lon1 = math.radians(ev_lat), math.radians(ev_lon)
    lat2, lon2 = math.radians(sta_lat), math.radians(sta_lon)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360


def _calc_gap(azimuths):
    """Maximum azimuthal gap (degrees) from a list of station azimuths."""
    if len(azimuths) < 2:
        return 360.0
    az = sorted(azimuths)
    gaps = [az[i + 1] - az[i] for i in range(len(az) - 1)]
    gaps.append(360.0 - az[-1] + az[0])
    return max(gaps)


def _event_link_col(picks):
    """Link column pick->event (gamma: event_index, real: event_id)."""
    for c in ("event_index", "event_id", "event"):
        if c in picks.columns:
            return c
    return None


def _station_col(picks):
    for c in ("station", "sta"):
        if c in picks.columns:
            return c
    return None


def _phase_col(picks):
    for c in ("phase", "type"):
        if c in picks.columns:
            return c
    return None


def _time_col(picks):
    for c in ("pick_time", "timestamp"):
        if c in picks.columns:
            return c
    return None


# ── Inter-station cross-correlation & stacking QC (ghost-event detector) ────
# Same technique as notebooks/event_station_stack_xcorr.ipynb: for one event,
# take the phase pick at every station and cross-correlate every station
# pair around the pick. A real earthquake gives a coherent onset shape
# across nearby stations (mean CC high); a mis-associated "ghost" event
# stitches together unrelated picks, so mean pairwise CC stays low.
def _fetch_station_trace(sds_client, network, station, t_pick, before, after,
                         bandpass=(1.0, 12.0), target_sr=100.0):
    """Fetch + preprocess (demean/taper/bandpass/resample) one Z-component window
    around a pick. Returns an obspy Trace, or None if unavailable/unusable."""
    from obspy import UTCDateTime

    from seiswork.utils.channels import read_best_waveform

    if not isinstance(t_pick, UTCDateTime):
        try:
            t_pick = UTCDateTime(str(t_pick))
        except Exception:
            return None
    try:
        st, _ = read_best_waveform(sds_client, network, station, t_pick - before, t_pick + after)
    except Exception:
        return None
    if not st:
        return None
    tr = st.select(component="Z")
    tr = (tr[0] if tr else st[0]).copy()
    try:
        tr.detrend("demean")
        tr.taper(0.05)
        nyq = tr.stats.sampling_rate / 2.0
        f_hi = min(bandpass[1], nyq * 0.95)
        if bandpass[0] < f_hi:
            tr.filter("bandpass", freqmin=bandpass[0], freqmax=f_hi, corners=4, zerophase=True)
        if abs(tr.stats.sampling_rate - target_sr) > 1e-6:
            tr.resample(target_sr)
    except Exception:
        return None
    if len(tr.data) < 4:
        return None
    return tr


def station_cc_matrix(stations_picks, sds_client, window_before=0.3, window_after=2.0,
                      max_shift_sec=0.5, bandpass=(1.0, 12.0), target_sr=100.0):
    """Full pairwise cross-correlation matrix between stations for one event.

    Parameters
    ----------
    stations_picks
        Iterable of (station, network, pick_time); pick_time as UTCDateTime
        or ISO string. One entry per station (first wins on duplicates).

    Returns a dict {stations, cc_matrix, lag_matrix, mean_cc, overall_mean_cc}
    (mean_cc keyed by station, excluding the diagonal), or None if fewer
    than 2 stations yield usable waveform.
    """
    from obspy.signal.cross_correlation import correlate, xcorr_max

    max_shift = int(round(max_shift_sec * target_sr))
    windows = {}
    for sta, net, t_pick in stations_picks:
        sta = str(sta)
        if sta in windows:
            continue
        tr = _fetch_station_trace(sds_client, str(net), sta, t_pick,
                                  window_before, window_after, bandpass, target_sr)
        if tr is None:
            continue
        y = tr.data.astype(float)
        windows[sta] = y - y.mean()

    stations = list(windows.keys())
    n = len(stations)
    if n < 2:
        return None

    cc_matrix  = np.full((n, n), np.nan)
    lag_matrix = np.full((n, n), np.nan)
    for i in range(n):
        cc_matrix[i, i] = 1.0
        lag_matrix[i, i] = 0.0
        for j in range(i + 1, n):
            a, b = windows[stations[i]], windows[stations[j]]
            cc_fun = correlate(a, b, max_shift)
            shift, val = xcorr_max(cc_fun, abs_max=False)
            cc_matrix[i, j] = cc_matrix[j, i] = val
            lag_matrix[i, j] = shift / target_sr
            lag_matrix[j, i] = -shift / target_sr

    off_diag = cc_matrix.copy()
    np.fill_diagonal(off_diag, np.nan)
    mean_cc = {sta: float(v) for sta, v in zip(stations, np.nanmean(off_diag, axis=1))}

    return {
        "stations": stations,
        "cc_matrix": cc_matrix.tolist(),
        "lag_matrix": lag_matrix.tolist(),
        "mean_cc": mean_cc,
        "overall_mean_cc": float(np.nanmean(list(mean_cc.values()))),
    }


def station_stack(stations_picks, sds_client, window_before=2.0, window_after=2.0,
                  bandpass=(1.0, 12.0), target_sr=100.0):
    """Aligned+normalized waveform overlay and linear stack for one event,
    same as the bottom panel of plot_record_section_and_stack() in
    notebooks/event_station_stack_xcorr.ipynb.

    Returns a dict {stations, rel_grid, aligned, stack} (aligned keyed by
    station, values aligned to rel_grid with None outside the trace's data
    range), or None if no station yields usable waveform.
    """
    dt = 1.0 / target_sr
    rel_grid = np.arange(-window_before, window_after, dt)

    aligned = {}
    for sta, net, t_pick in stations_picks:
        sta = str(sta)
        if sta in aligned:
            continue
        tr = _fetch_station_trace(sds_client, str(net), sta, t_pick,
                                  window_before, window_after, bandpass, target_sr)
        if tr is None:
            continue
        from obspy import UTCDateTime
        t_pick_utc = t_pick if isinstance(t_pick, UTCDateTime) else UTCDateTime(str(t_pick))
        rel_t = tr.times(reftime=t_pick_utc)
        y = tr.data.astype(float)
        mx = np.max(np.abs(y)) if np.max(np.abs(y)) > 0 else 1.0
        y_norm = y / mx
        aligned[sta] = np.interp(rel_grid, rel_t, y_norm, left=np.nan, right=np.nan)

    if not aligned:
        return None

    # Edges of rel_grid can fall outside every station's data range (all-NaN
    # column); expected, so nanmean's "empty slice" warning is just noise.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        stack = np.nanmean(np.array(list(aligned.values())), axis=0)
    return {
        "stations": list(aligned.keys()),
        "rel_grid": [round(float(v), 4) for v in rel_grid],
        "aligned": {sta: [None if np.isnan(v) else round(float(v), 5) for v in arr]
                   for sta, arr in aligned.items()},
        "stack": [None if np.isnan(v) else round(float(v), 5) for v in stack],
    }


def compute_event_cc(picks_grp, sds_client, station_col="station", phase_col="phase",
                     time_col="pick_time", phase="P",
                     window_before=0.3, window_after=2.0, max_shift_sec=0.5,
                     bandpass=(1.0, 12.0), target_sr=100.0):
    """Mean pairwise cross-correlation between stations for one event (used
    by `filter_catalog`'s min_cc ghost-event QC; see `station_cc_matrix` for
    the full per-pair matrix, e.g. for visualization).

    Returns None when fewer than 2 stations yield usable waveform, since
    there isn't enough evidence to judge the event, so it shouldn't be
    dropped.
    """
    rows = picks_grp[picks_grp[phase_col] == phase] if phase_col else picks_grp
    stations_picks = [(row[station_col], row["network"], row[time_col])
                      for _, row in rows.iterrows()]
    result = station_cc_matrix(stations_picks, sds_client, window_before, window_after,
                               max_shift_sec, bandpass, target_sr)
    return result["overall_mean_cc"] if result else None


def _num(v):
    try:
        f = float(v)
        return f if f == f else None    # discard NaN
    except (TypeError, ValueError):
        return None


def filter_catalog(cat_csv, out_cat, picks_csv=None, out_picks=None,
                   sta_coords=None,
                   min_lat=None, max_lat=None, min_lon=None, max_lon=None,
                   min_depth=None, max_depth=None,
                   min_mag=None, max_mag=None,
                   max_rms=None, min_phase=None, max_gap=None,
                   start_time=None, end_time=None,
                   min_cc=None, sds_path=None, cc_phase="P"):
    """Filter a CSV catalog, write filtered catalog (+ picks). Return stats.

    All criteria parameters are optional (None = not filtered). Matches
    filter_pha behavior: an event is only dropped if a value is present AND
    violates the threshold (e.g. events without magnitude aren't dropped
    by min_mag).

    min_cc / sds_path / cc_phase
        Ghost-event QC: drop events whose mean inter-station cross-correlation
        (same technique as notebooks/event_station_stack_xcorr.ipynb) falls
        below `min_cc`. Requires `sds_path` (SDS waveform archive) and picks
        with pick times. Events whose CC couldn't be computed (e.g. missing
        waveform, fewer than 2 usable stations) are not dropped.
    """
    cat = pd.read_csv(cat_csv)
    total = len(cat)
    if total == 0:
        cat.to_csv(out_cat, index=False)
        return {"total": 0, "passed": 0}

    # Time range
    dt = None
    if "datetime" in cat.columns and (start_time or end_time):
        dt = pd.to_datetime(cat["datetime"], errors="coerce", utc=True)
    st = pd.to_datetime(start_time, utc=True) if start_time else None
    et = pd.to_datetime(end_time, utc=True) if end_time else None

    # Phase count & triggering stations per event from picks
    phase_count, ev_stations, link_col = {}, {}, None
    picks = None
    if picks_csv and (min_phase is not None or max_gap is not None or min_cc is not None):
        try:
            picks = pd.read_csv(picks_csv)
        except Exception:
            picks = None
    if picks is not None and len(picks):
        link_col = _event_link_col(picks)
        scol = _station_col(picks)
        if link_col:
            for ev, grp in picks.groupby(link_col):
                k = str(ev)
                phase_count[k] = len(grp)
                if scol:
                    ev_stations[k] = set(grp[scol].astype(str))

    # Ghost-event QC: inter-station cross-correlation per event (opt-in, needs waveform)
    event_cc = {}
    if picks is not None and len(picks) and min_cc is not None and sds_path:
        pcol, tcol = _phase_col(picks), _time_col(picks)
        if link_col and scol and pcol and tcol:
            from obspy.clients.filesystem.sds import Client as SDSClient
            sds_client = SDSClient(sds_path)
            for ev, grp in picks.groupby(link_col):
                event_cc[str(ev)] = compute_event_cc(
                    grp, sds_client, station_col=scol, phase_col=pcol,
                    time_col=tcol, phase=cc_phase)

    keep = []
    dropped_by_cc = 0
    for i, row in cat.iterrows():
        eid = str(row.get("event_id"))
        lat, lon = _num(row.get("lat")), _num(row.get("lon"))
        dep, mag, rms = _num(row.get("depth_km")), _num(row.get("mag")), _num(row.get("rms"))

        if dt is not None:
            d = dt.iloc[i]
            if st is not None and pd.notna(d) and d < st:
                continue
            if et is not None and pd.notna(d) and d > et:
                continue
        if min_lat is not None and lat is not None and lat < min_lat:   continue
        if max_lat is not None and lat is not None and lat > max_lat:   continue
        if min_lon is not None and lon is not None and lon < min_lon:   continue
        if max_lon is not None and lon is not None and lon > max_lon:   continue
        if min_depth is not None and dep is not None and dep < min_depth: continue
        if max_depth is not None and dep is not None and dep > max_depth: continue
        if min_mag is not None and mag is not None and mag < min_mag:   continue
        if max_mag is not None and mag is not None and mag > max_mag:   continue
        if max_rms is not None and rms is not None and rms > max_rms:   continue

        if min_phase is not None:
            n = phase_count.get(eid)
            if n is None:
                n = _num(row.get("nsta"))
            if n is not None and n < min_phase:
                continue

        if max_gap is not None:
            gap = _num(row.get("gap"))
            if gap is None and sta_coords and lat is not None and lon is not None:
                stas = ev_stations.get(eid, set())
                az = [_calc_azimuth(lat, lon, *sta_coords[s])
                      for s in stas if s in sta_coords]
                gap = _calc_gap(az) if az else None
            if gap is not None and gap > max_gap:
                continue

        if min_cc is not None:
            cc = event_cc.get(eid)
            if cc is not None and cc < min_cc:
                dropped_by_cc += 1
                continue

        keep.append(i)

    fcat = cat.loc[keep].copy()
    if event_cc:
        fcat["qc_cc"] = fcat["event_id"].astype(str).map(event_cc)
    fcat.to_csv(out_cat, index=False)

    # Filtered picks (only events that passed), so downstream steps still have picks
    if picks is not None and out_picks and link_col:
        kept = set(str(e) for e in fcat["event_id"])
        fp = picks[picks[link_col].astype(str).isin(kept)]
        fp.to_csv(out_picks, index=False)

    return {"total": total, "passed": int(len(fcat)), "dropped_by_cc": dropped_by_cc}

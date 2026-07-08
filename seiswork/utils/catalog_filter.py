"""
SeisWork — Catalog filter  (by HakimBMKG)

Adapts the logic of `simulflow.visualization.MapPlot.filter_pha` but operates
directly on SeisWork CSV catalogs (columns: event_id, datetime, lat, lon,
depth_km, mag, rms, nsta, gap, method) so the output remains in the same format
as other catalogs and can be used by all pipeline functions.

Criteria: lat/lon/depth bounds, magnitude (min/max), max RMS, min phase count,
max azimuthal gap (degrees), and time range (start/end).

`min_phase` is computed from the per-event pick count (picks_associated.csv);
falls back to the `nsta` column when picks are unavailable. `max_gap` uses the
`gap` column from the catalog when present; if empty (e.g. GaMMA), it is
computed from station azimuths (requires station coordinates) — identical to
the simulflow filter_pha behaviour.
"""
import math
import pandas as pd


# ── Azimuth & gap (verbatim port from simulflow MapPlot) ─────────────────────
def _calc_azimuth(ev_lat, ev_lon, sta_lat, sta_lon):
    """Azimuth (0-360°) from event to station."""
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
    """Link column pick→event (gamma: event_index, real: event_id)."""
    for c in ("event_index", "event_id", "event"):
        if c in picks.columns:
            return c
    return None


def _station_col(picks):
    for c in ("station", "sta"):
        if c in picks.columns:
            return c
    return None


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
                   start_time=None, end_time=None):
    """Filter a CSV catalog → write filtered catalog (+ picks). Return statistics.

    All criteria parameters are optional (None = not filtered). Matches
    filter_pha behaviour: an event is only dropped if a value is present AND
    violates the threshold (events without magnitude are not dropped by min_mag, etc.).
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
    if picks_csv and (min_phase is not None or max_gap is not None):
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

    keep = []
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

        keep.append(i)

    fcat = cat.loc[keep]
    fcat.to_csv(out_cat, index=False)

    # Filtered picks (only events that passed) → downstream steps still have picks
    if picks is not None and out_picks and link_col:
        kept = set(str(e) for e in fcat["event_id"])
        fp = picks[picks[link_col].astype(str).isin(kept)]
        fp.to_csv(out_picks, index=False)

    return {"total": total, "passed": int(len(fcat))}

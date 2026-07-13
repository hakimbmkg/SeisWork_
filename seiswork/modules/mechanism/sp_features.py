"""
SeisWork - S/P amplitude ratio + SNR features for FocoNet_Full - by HakimBMKG

FocoNet_Full uses 11 features per station (all log10-scaled except polarity):
  0  polarity       - P first-motion polarity (+/-1 or +/-0.5)
  1  max{S}/|P|     - log10(max(Sr,Sz,St) / Pz)
  2  Sr/Pr          - log10(S_radial / P_radial)
  3  Sr/Pz          - log10(S_radial / P_vertical)
  4  Sz/Pr          - log10(S_vertical / P_radial)
  5  Sz/Pz          - log10(S_vertical / P_vertical)
  6  St/Pr          - log10(S_transverse / P_radial)
  7  St/Pz          - log10(S_transverse / P_vertical)
  8  Pr/Nr          - log10(P_radial SNR)
  9  Pz/Nz          - log10(P_vertical SNR)
  10 St/Nt          - log10(S_transverse SNR)

Channels: vertical Z, horizontal N + E rotated to R (radial), T (transverse).
Band auto-detected per station (HH > BH > EH > SH; see utils/channels.py)
since a network's stations don't all run the same sensor.
Back-azimuth (station->event direction) drives the N/E -> R/T rotation.
"""

import numpy as np
import pandas as pd
from obspy import UTCDateTime
from obspy.clients.filesystem.sds import Client as SDSClient
from obspy.geodetics import gps2dist_azimuth

from seiswork.utils.channels import BAND_PRIORITY

# Window parameters (seconds)
NOISE_PRE  = 1.5   # noise window: t_P - NOISE_PRE  ->  t_P - NOISE_POST
NOISE_POST = 0.5
P_WIN      = 0.8   # P-amplitude window: t_P  ->  t_P + P_WIN
S_WIN      = 1.0   # S-amplitude window: t_S  ->  t_S + S_WIN
VP_VS      = 1.73  # fallback Vp/Vs ratio when no S pick

_EPS      = 1e-9
_LOG_CLIP = (-3.0, 3.0)   # clip log10 ratios to [-3, 3]


def _to_utc(t) -> UTCDateTime:
    """UTCDateTime() rejects pandas-formatted strings
    ("YYYY-MM-DD HH:MM:SS.ffffff+00:00") - normalize via pandas first."""
    return UTCDateTime(pd.Timestamp(t).to_pydatetime())

SP_FEAT_COLS = [
    "sp_max", "Sr_Pr", "Sr_Pz",
    "Sz_Pr",  "Sz_Pz",
    "St_Pr",  "St_Pz",
    "Pr_Nr",  "Pz_Nz", "St_Nt",
]


def _safe_log(num, den):
    """log10(num/den), clipped to LOG_CLIP, 0.0 on invalid input."""
    if den is None or den < _EPS:
        return 0.0
    if num is None or num < _EPS:
        return float(_LOG_CLIP[0])
    return float(np.clip(np.log10(float(num) / float(den)), *_LOG_CLIP))


def _peak_amp(data, sr, t_trace_start, t_win_start, t_win_end):
    """Peak absolute amplitude in [t_win_start, t_win_end]."""
    i0 = max(0, int((t_win_start - t_trace_start) * sr))
    i1 = min(len(data), int((t_win_end   - t_trace_start) * sr))
    if i0 >= i1:
        return None
    seg = data[i0:i1]
    return float(np.max(np.abs(seg))) if len(seg) > 0 else None


def _rotate_ne_rt(n_data, e_data, baz_deg):
    """Rotate North/East arrays to Radial/Transverse using back-azimuth (deg)."""
    baz = np.radians(baz_deg)
    r =  n_data * np.cos(baz) + e_data * np.sin(baz)
    t = -n_data * np.sin(baz) + e_data * np.cos(baz)
    return r, t


def compute_sp_features(picks_p: pd.DataFrame, picks_s: pd.DataFrame,
                        sds_path: str, station_locs: dict, cat_df: pd.DataFrame,
                        network: str = "7G", band: str | None = None,
                        progress_every: int = 200) -> pd.DataFrame:
    """
    Compute 10 S/P + SNR features (sp_max ... St_Nt) per (event_id, station).

    Parameters
    ----------
    picks_p      : P-pick DataFrame - columns: event_id, network, station, pick_time
    picks_s      : S-pick DataFrame - same columns (may be partial)
    sds_path     : path to SDS archive root
    station_locs : {station_code: (lat, lon)}
    cat_df       : event catalog - columns: event_id, lat, lon, depth_km, datetime
    network      : fallback network code
    band         : explicit 2-letter band+instrument code (e.g. 'HH') to force
                   for every station, or None (default) to auto-try the
                   standard HH > BH > EH > SH priority per station - the Z/N/E
                   trio is always fetched from the same band.
    Returns DataFrame columns: event_id, station, ok, sp_max ... St_Nt
    """
    sds = SDSClient(sds_path)

    # S-pick lookup: (event_id, station) -> pick_time string
    s_lut = {}
    if picks_s is not None and len(picks_s):
        pt_col = "pick_time" if "pick_time" in picks_s.columns else "timestamp"
        for _, r in picks_s.iterrows():
            t = r.get(pt_col) if hasattr(r, "get") else r[pt_col]
            s_lut[(str(r.event_id), str(r.station))] = t

    # Origin-time lookup for S-time estimation when no S pick
    orig_lut = {}
    for _, r in cat_df.iterrows():
        orig_lut[str(r["event_id"])] = {
            "lat":       float(r.get("lat", 0)),
            "lon":       float(r.get("lon", 0)),
            "depth_km":  float(r.get("depth_km", 10)),
            "origin_time": str(r.get("datetime", "")),
        }

    rows = []
    pt_col_p = "pick_time" if "pick_time" in picks_p.columns else "timestamp"
    n = len(picks_p)

    for i, (_, rp) in enumerate(picks_p.iterrows()):
        eid = str(rp.event_id)
        sta = str(rp.station)
        net = str(rp.network)
        raw_t = rp.get(pt_col_p) if hasattr(rp, "get") else rp[pt_col_p]
        t_p = _to_utc(raw_t)

        # Determine S time
        s_key = (eid, sta)
        if s_key in s_lut:
            t_s = _to_utc(s_lut[s_key])
        else:
            ev = orig_lut.get(eid, {})
            try:
                dt_p = t_p - _to_utc(ev.get("origin_time", ""))
                t_s  = t_p + dt_p * (VP_VS - 1)
            except Exception:
                t_s = t_p + 2.0

        t_fetch_start = t_p - NOISE_PRE  - 0.2
        t_fetch_end   = t_s + S_WIN      + 0.3

        # Fetch Z/N/E from the same band (a station's Z/N/E must match - never
        # mix e.g. HHZ with SHN), trying bands in priority order until all
        # three components are present.
        st_z = st_n = st_e = None
        for b in ([band] if band else BAND_PRIORITY):
            try:
                st_z = sds.get_waveforms(net, sta, "", f"{b}Z", t_fetch_start, t_fetch_end)
                st_n = sds.get_waveforms(net, sta, "", f"{b}N", t_fetch_start, t_fetch_end)
                st_e = sds.get_waveforms(net, sta, "", f"{b}E", t_fetch_start, t_fetch_end)
            except Exception:
                st_z = st_n = st_e = None
                continue
            if st_z and st_n and st_e:
                break

        if not st_z or not st_n or not st_e:
            rows.append({"event_id": eid, "station": sta, "ok": False})
            continue

        try:
            tr_z = st_z[0].copy().detrend("demean")
            tr_n = st_n[0].copy().detrend("demean")
            tr_e = st_e[0].copy().detrend("demean")

            sr = tr_z.stats.sampling_rate
            t0 = float(tr_z.stats.starttime)

            # Align N and E sampling rate with Z
            if tr_n.stats.sampling_rate != sr:
                tr_n.resample(sr)
            if tr_e.stats.sampling_rate != sr:
                tr_e.resample(sr)

            min_len = min(tr_z.stats.npts, tr_n.stats.npts, tr_e.stats.npts)
            z = tr_z.data[:min_len].astype(float)
            n_arr = tr_n.data[:min_len].astype(float)
            e_arr = tr_e.data[:min_len].astype(float)

            # Back-azimuth for N/E -> R/T rotation
            ev    = orig_lut.get(eid, {})
            ev_lat, ev_lon = ev.get("lat", 0), ev.get("lon", 0)
            sta_lat, sta_lon = station_locs.get(sta, (ev_lat, ev_lon))
            _, _, baz = gps2dist_azimuth(ev_lat, ev_lon, sta_lat, sta_lon)
            r, t = _rotate_ne_rt(n_arr, e_arr, baz)

            t_pf = float(t_p)
            t_sf = float(t_s)

            # Noise window amplitudes
            Nz = _peak_amp(z, sr, t0, t_pf - NOISE_PRE, t_pf - NOISE_POST)
            Nr = _peak_amp(r, sr, t0, t_pf - NOISE_PRE, t_pf - NOISE_POST)
            Nt = _peak_amp(t, sr, t0, t_pf - NOISE_PRE, t_pf - NOISE_POST)

            # P-window amplitudes
            Pz = _peak_amp(z, sr, t0, t_pf, t_pf + P_WIN)
            Pr = _peak_amp(r, sr, t0, t_pf, t_pf + P_WIN)

            # S-window amplitudes
            Sz = _peak_amp(z, sr, t0, t_sf, t_sf + S_WIN)
            Sr = _peak_amp(r, sr, t0, t_sf, t_sf + S_WIN)
            St = _peak_amp(t, sr, t0, t_sf, t_sf + S_WIN)

            # max{S}/|P|
            s_vals = [x for x in (Sr, Sz, St) if x is not None]
            S_max  = max(s_vals) if s_vals else None

            rows.append({
                "event_id": eid, "station": sta, "ok": True,
                "sp_max": _safe_log(S_max, Pz),
                "Sr_Pr":  _safe_log(Sr,    Pr),
                "Sr_Pz":  _safe_log(Sr,    Pz),
                "Sz_Pr":  _safe_log(Sz,    Pr),
                "Sz_Pz":  _safe_log(Sz,    Pz),
                "St_Pr":  _safe_log(St,    Pr),
                "St_Pz":  _safe_log(St,    Pz),
                "Pr_Nr":  _safe_log(Pr,    Nr),
                "Pz_Nz":  _safe_log(Pz,    Nz),
                "St_Nt":  _safe_log(St,    Nt),
            })

        except Exception:
            rows.append({"event_id": eid, "station": sta, "ok": False})

        if progress_every and (i + 1) % progress_every == 0:
            print(f"[FOCONET-FULL] S/P features: {i+1}/{n} picks...", flush=True)

    return pd.DataFrame(rows)

"""
SeisWork - P-wave first-motion polarity picker (for focal mechanisms) - by HakimBMKG

PhaseNet/EQTransformer pickers only output P/S onset time + probability, not
first-motion polarity - but SKHASH/HASH-style focal mechanism inversion needs
polarity (up/compressional vs down/dilatational) per P pick. This derives it
directly from the raw vertical-component (Z) waveform around each P pick:
read a short pre-pick noise window + post-pick signal window, walk forward
from the pick until the trace exceeds `snr_min` x the noise std, and take the
sign of that first clear deflection.

Relies on the near-universal SEED-standard convention that a vertical
channel with dip=-90 reports a positive count for upward ground motion, so
the raw count sign is the polarity directly - no instrument response needed.
"""

import numpy as np
import pandas as pd
from obspy import UTCDateTime
from obspy.clients.filesystem.sds import Client as SDSClient

from seiswork.utils.channels import read_best_waveform


def first_motion_polarity(sds: SDSClient, net: str, sta: str, cha,
                           t_pick, noise_win=1.0, sig_win=0.4, snr_min=2.5):
    """Return (polarity, snr, status) for one P pick.
    polarity: +1.0 (up/compressional), -1.0 (down/dilatational), or None.
    status: 'ok' | 'no-data' | 'too-short' | 'flat-noise' | 'low-snr' | 'err:...'
    `cha`: an explicit channel code, a list of candidate codes to try in
    order, or None to auto-try the standard HH > BH > EH > SH band priority
    - stations on the same network don't all run the same sensor band.
    """
    # t_pick may arrive as a pandas-formatted string ("YYYY-MM-DD HH:MM:SS.ffffff+00:00")
    # which UTCDateTime's parser rejects - normalize via pandas first.
    t0 = UTCDateTime(pd.Timestamp(t_pick).to_pydatetime())
    try:
        st, _cha = read_best_waveform(sds, net, sta, t0 - noise_win, t0 + sig_win, channel=cha)
    except Exception as e:
        return None, None, f"err:{e}"
    if not st:
        return None, None, "no-data"
    tr = st[0].copy()
    tr.detrend("demean")
    sr = tr.stats.sampling_rate
    n_noise = int(noise_win * sr)
    if tr.stats.npts < n_noise + 5:
        return None, None, "too-short"
    noise_std = np.std(tr.data[:n_noise])
    if noise_std <= 0:
        return None, None, "flat-noise"
    sig = tr.data[n_noise:]
    thresh = snr_min * noise_std
    idx = np.argmax(np.abs(sig) >= thresh)
    if np.abs(sig[idx]) < thresh:
        return None, None, "low-snr"
    val = sig[idx]
    snr = abs(val) / noise_std
    return (1.0 if val > 0 else -1.0), snr, "ok"


def compute_polarities(picks_p: pd.DataFrame, sds_path: str, channel=None,
                        noise_win=1.0, sig_win=0.4, snr_min=2.5,
                        confident_snr=4.0, progress_every=200) -> pd.DataFrame:
    """picks_p: DataFrame with columns [event_id, network, station, pick_time]
    (P phase only - filter before calling). Returns a DataFrame with columns
    [event_id, station, network, p_polarity, snr, status] - p_polarity is
    signed (+/-1.0 confident if snr>=confident_snr, else +/-0.5), NaN if the
    pick couldn't be read confidently (caller should drop those rows).

    `channel`: an explicit SEED channel code (e.g. 'HHZ') to force one band
    for every station, or None (default) to auto-try the standard
    HH > BH > EH > SH priority per station - needed because a real network
    usually mixes broadband and short-period sensors."""
    sds = SDSClient(sds_path)
    rows = []
    n = len(picks_p)
    for i, (_, row) in enumerate(picks_p.iterrows()):
        pol, snr, status = first_motion_polarity(
            sds, row.network, row.station, channel, row.pick_time,
            noise_win=noise_win, sig_win=sig_win, snr_min=snr_min)
        weight = None
        if pol is not None:
            weight = pol * (1.0 if snr >= confident_snr else 0.5)
        if progress_every and (i + 1) % progress_every == 0:
            print(f"[MECH] polarity: {i + 1}/{n} picks processed...", flush=True)
        rows.append({
            "event_id": row.event_id, "station": row.station,
            "network": row.network, "p_polarity": weight,
            "snr": snr, "status": status,
        })
    return pd.DataFrame(rows)

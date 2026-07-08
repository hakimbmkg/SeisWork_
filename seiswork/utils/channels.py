"""
Shared seismic channel-selection helpers — by HakimBMKG

SEED/FDSN channel codes are 3 letters: [band][instrument][orientation].
A network's stations don't all run the same sensor — broadband HH/BH
instruments mix with short-period EH/SH ones on the same network (e.g. IA
has several SHZ-only sites) — so code that needs "the" channel for a
station must check what that station actually has instead of assuming one
band code everywhere.

Band-code priority used here, best (broadest band / highest sample rate)
first, per the SEED convention: HH (High Broadband) > BH (Broadband) >
EH (Extremely Short Period) > SH (Short Period).
"""
from __future__ import annotations

BAND_PRIORITY = ["HH", "BH", "EH", "SH"]


def pick_preferred_channel(available, orientation: str = "Z",
                            band_priority: list[str] | None = None) -> str | None:
    """Pick the best channel code for one orientation out of a station's
    actual channel list (e.g. from an inventory), following the standard
    band priority (default HH > BH > EH > SH). Falls back to any channel
    with the same orientation, then to whatever is first in `available`.
    Returns None if `available` is empty."""
    available = list(available)
    if not available:
        return None
    for band in (band_priority or BAND_PRIORITY):
        code = f"{band}{orientation}"
        if code in available:
            return code
    same_orientation = [c for c in available if c.endswith(orientation)]
    if same_orientation:
        return same_orientation[0]
    return available[0]


def channel_search_order(orientation: str = "Z", band_priority: list[str] | None = None,
                          wildcard: bool = True) -> list[str]:
    """Build an ordered list of channel codes/patterns to try against an
    SDS/FDSN client for one orientation when the station's actual channel
    list isn't known ahead of time, following the standard band priority
    (default HH > BH > EH > SH). When `wildcard` is set, a same-band
    wildcard (e.g. 'HH?') is appended after each exact code as a last
    resort."""
    bands = band_priority or BAND_PRIORITY
    order = [f"{b}{orientation}" for b in bands]
    if wildcard:
        order += [f"{b}?" for b in bands]
    return order


def read_best_waveform(client, net: str, sta: str, t_start, t_end,
                        orientation: str = "Z", band_priority: list[str] | None = None,
                        loc: str = "*", channel=None):
    """Try get_waveforms() against a station's actual channels, following
    the standard band priority (default HH > BH > EH > SH), and return
    (stream, channel_code_used). Returns (None, None) if nothing is found.

    `channel`: an explicit channel code/pattern, or list of them, to try
    instead of the auto-detected priority order (skips other bands)."""
    if isinstance(channel, str):
        candidates = [channel]
    elif channel:
        candidates = list(channel)
    else:
        candidates = channel_search_order(orientation, band_priority)
    for cha in candidates:
        try:
            st = client.get_waveforms(net, sta, loc, cha, t_start, t_end)
        except Exception:
            continue
        if st:
            return st, cha
    return None, None

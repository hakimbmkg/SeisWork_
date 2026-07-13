"""
Shared seismic channel-selection helpers - by HakimBMKG

SEED/FDSN channel codes are 3 letters: [band][instrument][orientation].
Stations on the same network can run different sensors (e.g. some IA
stations are SHZ-only), so code needing "the" channel for a station must
check what that station actually has, not assume one band code everywhere.

Band priority here, best first: HH (High Broadband) > BH (Broadband) >
EH (Extremely Short Period) > SH (Short Period).
"""
from __future__ import annotations

BAND_PRIORITY = ["HH", "BH", "EH", "SH"]


def pick_preferred_channel(available, orientation: str = "Z",
                            band_priority: list[str] | None = None) -> str | None:
    """Pick the best channel code for one orientation from a station's actual
    channel list, using band priority (default HH > BH > EH > SH). Falls back
    to any channel with the same orientation, then the first in `available`.
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
    SDS/FDSN client when the station's actual channels aren't known ahead of
    time, using band priority (default HH > BH > EH > SH). If `wildcard`,
    a same-band wildcard (e.g. 'HH?') is appended after each exact code."""
    bands = band_priority or BAND_PRIORITY
    order = [f"{b}{orientation}" for b in bands]
    if wildcard:
        order += [f"{b}?" for b in bands]
    return order


def read_best_waveform(client, net: str, sta: str, t_start, t_end,
                        orientation: str = "Z", band_priority: list[str] | None = None,
                        loc: str = "*", channel=None):
    """Try get_waveforms() against a station's actual channels using band
    priority (default HH > BH > EH > SH), returning (stream, channel_used).
    Returns (None, None) if nothing is found.

    `channel`: explicit channel code/pattern (or list) to try instead of
    the auto-detected priority order (skips other bands)."""
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

"""
SeisWork — StationXML PolesZerosResponseStage normalization_factor (A0) fix.
Author: HakimBMKG

Real-world finding (network 7G, 2026-06-22): several FDSN-served inventories
ship a placeholder A0 (e.g. 1.0) for custom/hand-entered sensor responses
(e.g. SM-6 geophones) instead of the value the stage's own poles/zeros/
normalization_frequency actually require — confirmed wrong both in a local
StationXML copy AND the upstream FDSN source itself, so it is not just a
stale-cache problem. ObsPy's simulate_seismometer()/remove_response() both
trust this value blindly, so a wrong A0 silently biases every amplitude (and
therefore every magnitude) computed from that channel.

This recomputes A0 directly from each stage's own poles/zeros so the result
is correct regardless of what the inventory (or its FDSN source) claims —
no need to know which sensor type is affected; stages that are already
correct (e.g. Trillium Compact BB*) come back unchanged.
"""

import math


def recompute_a0(poles, zeros, norm_freq):
    """SEED/FDSN normalization: the only A0 for which the poles-zeros
    transfer function evaluates to unity gain at the stage's own
    `normalization_frequency` — i.e. |A0 * H_pz(j*2*pi*norm_freq)| = 1."""
    w = 2 * math.pi * norm_freq
    val = 1.0 + 0j
    for z in zeros:
        val *= (1j * w - z)
    for p in poles:
        val /= (1j * w - p)
    return 1.0 / abs(val)


def fix_inventory_normalization(inv, tol=0.01, channel_codes=None):
    """Walk every PolesZerosResponseStage in `inv`, recompute A0 from its own
    poles/zeros, and overwrite `normalization_factor` in place if it deviates
    from the stored value by more than `tol` (default 1%) — or is missing.

    channel_codes: optional iterable to restrict which channels are checked
    (e.g. {'HHZ'}); None checks all channels.

    Returns a list of (station, channel, old_a0, new_a0) for anything fixed,
    so callers can log what was corrected.
    """
    fixed = []
    for net in inv:
        for sta in net:
            for ch in sta:
                if channel_codes and ch.code not in channel_codes:
                    continue
                if not ch.response or not ch.response.response_stages:
                    continue
                for stage in ch.response.response_stages:
                    poles = getattr(stage, "poles", None)
                    norm_freq = getattr(stage, "normalization_frequency", None)
                    if not poles or not norm_freq:
                        continue
                    zeros = getattr(stage, "zeros", None) or []
                    old_a0 = stage.normalization_factor
                    new_a0 = recompute_a0(poles, zeros, norm_freq)
                    if not old_a0 or abs(new_a0 - old_a0) / new_a0 > tol:
                        stage.normalization_factor = new_a0
                        fixed.append((sta.code, ch.code, old_a0, new_a0))
    return fixed

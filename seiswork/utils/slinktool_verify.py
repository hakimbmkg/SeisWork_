"""
slinktool_verify.py - by HakimBMKG

Cross-check an inventory's per-station default_channel against what a
SeedLink server actually carries right now, using EarthScope's slinktool
(bundled + compiled by install.sh Step 3, core/bin/slinktool; falls back
to a system slinktool such as the one SeisComP ships, on PATH).

Why: a station's inventory entry can go stale relative to the live
acquisition chain (sensor swapped, channel re-bound) without the exported
StationXML ever being regenerated. See get_stations()'s epoch handling in
_online_stations.py for the companion fix (always take the most current
epoch from the XML). Even a current epoch can still disagree with what the
SeedLink server transmits, which only this live query can catch.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess


def find_slinktool() -> str:
    """Repo-bundled slinktool first (portable, always known-good), then
    whatever's on PATH (e.g. the copy SeisComP ships)."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    bundled = os.path.join(repo_root, "core", "bin", "slinktool")
    if os.path.exists(bundled):
        return bundled
    return shutil.which("slinktool") or ""


# ── Per-station resolved-channel cache ──────────────────────────────────────
# Persists every confirmed live channel (from a successful slinktool query)
# per config, so a station whose true band isn't "BH" - e.g. AAI, which only
# transmits SH - still gets the right one when a later query times out (a
# large aggregator SeedLink server won't always answer -Q within the short
# connect-time budget). Without this, the last-resort fallback would force
# "BH" on AAI too and it would go silent, even though its real channel was
# already discovered once before.
def _cache_path(cfg_id: str) -> str:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return os.path.join(repo_root, "work", "gui_configs", cfg_id, "config_station.json")


def load_channel_cache(cfg_id: str) -> dict[tuple[str, str], str]:
    path = _cache_path(cfg_id)
    if not cfg_id or not os.path.exists(path):
        return {}
    try:
        raw = json.loads(open(path, encoding="utf-8").read())
        return {tuple(k.split(".", 1)): v for k, v in raw.items() if "." in k}
    except Exception:
        return {}


def save_channel_cache(cfg_id: str, updates: dict[tuple[str, str], str]) -> None:
    """Merge `updates` ({(net,sta): channel}) into the cache file. Never
    removes previously-confirmed entries a fresh (possibly partial) query
    didn't touch."""
    if not cfg_id or not updates:
        return
    path = _cache_path(cfg_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = load_channel_cache(cfg_id)
        existing.update(updates)
        raw = {f"{net}.{sta}": cha for (net, sta), cha in existing.items()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, sort_keys=True)
    except Exception:
        pass


def query_streams(host: str, port: int, timeout: float = 30.0) -> dict[tuple[str, str], set[str]]:
    """Run `slinktool -Q host:port` and return {(net, sta): {channel, ...}}.
    Raises RuntimeError if slinktool isn't available or the query fails."""
    exe = find_slinktool()
    if not exe:
        raise RuntimeError("slinktool not found (run install.sh step 3, or install SeisComP)")

    try:
        result = subprocess.run(
            [exe, "-Q", f"{host}:{port}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"slinktool -Q {host}:{port} timed out after {timeout:.0f}s")
    except Exception as e:
        raise RuntimeError(f"slinktool failed to run: {e}")

    if result.returncode != 0:
        raise RuntimeError(f"slinktool -Q {host}:{port} failed: {result.stderr.strip()[:300]}")

    # Each line: NET STA [LOC] CHA TYPE START_DATE - END_DATE
    # The channel is always the token immediately before the D/E type marker.
    streams: dict[tuple[str, str], set[str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        net, sta = parts[0], parts[1]
        cha = None
        for i, tok in enumerate(parts):
            if tok in ("D", "E") and i > 0:
                cha = parts[i - 1]
                break
        if not cha:
            continue
        streams.setdefault((net, sta), set()).add(cha)
    return streams


def resolve_live_channels(streams: list[tuple], host: str, port: int,
                           timeout: float = 10.0, cfg_id: str | None = None) -> list[tuple]:
    """For each (net, sta, loc, cha) in `streams`, replace cha with the
    single best-live band (by BAND_PRIORITY: HH > BH > EH > SH) confirmed via
    a slinktool query, so a station with several bands simultaneously live
    (e.g. BH, HH, and SH all transmitting) only gets one subscribed, not all
    at once. Every confirmed result is saved to the per-config channel cache
    (config_station.json) as it's found.

    When slinktool is unavailable or the query times out (a large aggregator
    SeedLink server, thousands of streams, can take 30s+ to enumerate via -Q,
    not worth blocking Connect for), each station falls back to its own
    cached channel from a past successful query, if one exists (e.g. AAI,
    which only transmits SH; forcing "BH" would make it go silent even
    though its real channel was already discovered before). Only a station
    with no live query and no cache entry gets the last-resort "BH" force,
    since a deterministic single band beats a wildcard that could pull in
    more than one simultaneously-live band. Use "Verify Channels"
    (verify_channels(), longer timeout) to populate the cache for a slow
    server ahead of time."""
    cache = load_channel_cache(cfg_id) if cfg_id else {}
    try:
        live = query_streams(host, port, timeout=timeout)
    except Exception:
        return [(net, sta, loc, cache.get((net, sta)) or f"BH{cha[-1] if cha else 'Z'}")
                for net, sta, loc, cha in streams]

    from seiswork.utils.channels import BAND_PRIORITY
    resolved = []
    cache_updates: dict[tuple[str, str], str] = {}
    for net, sta, loc, cha in streams:
        orientation = cha[-1] if cha else "Z"
        available_bands = {c[:2] for c in live.get((net, sta), set())
                           if len(c) == 3 and c[-1] == orientation}
        band = next((b for b in BAND_PRIORITY if b in available_bands), None)
        if band:
            confirmed = f"{band}{orientation}"
            cache_updates[(net, sta)] = confirmed
            resolved.append((net, sta, loc, confirmed))
        else:
            # Not found in this query (station offline, or a partial/failed
            # enumeration): fall back to a past confirmed channel before
            # resorting to a blind "BH".
            resolved.append((net, sta, loc, cache.get((net, sta)) or f"BH{orientation}"))
    if cfg_id:
        save_channel_cache(cfg_id, cache_updates)
    return resolved


def verify_channels(stations: list[dict], host: str, port: int,
                     timeout: float = 60.0, cfg_id: str | None = None) -> dict:
    """Compare each station's default_channel (from get_stations()) against
    the live SeedLink stream list. Returns a report:
    {matched, mismatched: [{net,sta,expected,live}], missing: [{net,sta,expected}]}.

    Also populates the per-config channel cache (config_station.json, see
    resolve_live_channels) with the best-live band for every station found.
    This has a much longer timeout than the connect-time resolution, so
    running it once ahead of time on a slow/large SeedLink server warms up
    the cache for a later, short-timeout connect() to fall back on."""
    live = query_streams(host, port, timeout=timeout)
    from seiswork.utils.channels import BAND_PRIORITY
    matched = 0
    mismatched = []
    missing = []
    cache_updates: dict[tuple[str, str], str] = {}
    for s in stations:
        key = (s.get("net"), s.get("sta"))
        expected = s.get("default_channel")
        if key not in live:
            missing.append({"net": key[0], "sta": key[1], "expected": expected})
            continue
        if expected in live[key]:
            matched += 1
        else:
            mismatched.append({"net": key[0], "sta": key[1], "expected": expected,
                               "live": sorted(live[key])})
        orientation = expected[-1] if expected else "Z"
        available_bands = {c[:2] for c in live[key] if len(c) == 3 and c[-1] == orientation}
        band = next((b for b in BAND_PRIORITY if b in available_bands), None)
        if band:
            cache_updates[key] = f"{band}{orientation}"
    if cfg_id:
        save_channel_cache(cfg_id, cache_updates)
    return {
        "ok": True,
        "host": host, "port": port,
        "total": len(stations),
        "matched": matched,
        "mismatched": mismatched,
        "missing": missing,
        "cached": len(cache_updates),
    }

"""
_online_stations.py - by HakimBMKG
Parse a StationXML/FDSNXML inventory into a station list for Online mode.
"""

from __future__ import annotations
import os
from pathlib import Path

from seiswork.utils.channels import pick_preferred_channel


# Known default inventory locations. The REPO-RELATIVE config/inventory.xml
# is preferred (portable); the SEISWORK_INVENTORY env override ranks highest.
_REPO_ROOT = Path(__file__).resolve().parents[2]   # seiswork/web/file.py -> repo root
_DEFAULT_INVENTORY_CANDIDATES = [
    *([Path(os.environ["SEISWORK_INVENTORY"])] if os.environ.get("SEISWORK_INVENTORY") else []),
    _REPO_ROOT / "config" / "inventory.xml",
    Path.home() / "Downloads" / "Package_1782193650286.xml",
    Path.home() / "datas" / "SDS_SCART" / "metadata" / "IA_metadata.xml",
    Path.home() / "data" / "ResponseIA7G.xml",
]


def find_default_inventory() -> str | None:
    """Return path to first usable inventory file found."""
    for p in _DEFAULT_INVENTORY_CANDIDATES:
        if p.exists() and p.stat().st_size > 1024:
            return str(p)
    return None


def get_stations(
    inventory_path: str,
    lat_min: float | None = None,
    lat_max: float | None = None,
    lon_min: float | None = None,
    lon_max: float | None = None,
) -> list[dict]:
    """
    Parse StationXML and return the station list as a list of dicts.
    Optional filtering by a lat/lon bounding box.

    Return: [{net, sta, lat, lon, elev, name, channels, default_channel}]
    """
    try:
        from obspy import read_inventory
    except ImportError:
        raise RuntimeError("obspy is not available")

    inv = read_inventory(inventory_path)
    stations = []

    # A station can have several epochs in the XML (moved location, re-survey,
    # metadata revision), each its own Station element. Taking whichever comes
    # first in the file can pick a stale epoch, so group by (net,sta) and keep
    # only the most current one: prefer an active epoch (no end_date), else the
    # one with the latest start_date.
    epochs: dict[tuple[str, str], list] = {}
    for net in inv:
        for sta in net:
            epochs.setdefault((net.code, sta.code), []).append((net, sta))

    def _epoch_rank(pair):
        _, s = pair
        is_active = s.end_date is None
        start_ts = s.start_date.timestamp if s.start_date else 0.0
        return (is_active, start_ts)

    for (net_code, sta_code), pairs in epochs.items():
        net, sta = max(pairs, key=_epoch_rank)
        lat = sta.latitude or 0.0
        lon = sta.longitude or 0.0
        elev = sta.elevation or 0.0

        # Filter bounding box
        if lat_min is not None and not (lat_min <= lat <= lat_max):
            continue
        if lon_min is not None and not (lon_min <= lon <= lon_max):
            continue

        # Preferred channel for this station, from what it actually has:
        # standard band priority HH > BH > EH > SH (see utils/channels.py).
        all_ch = list(dict.fromkeys(ch.code for ch in sta.channels))
        pref = pick_preferred_channel(all_ch, "Z") or "HHZ"

        # Location code of the preferred channel, required for response removal
        # and magnitude. Stations mix '' and '00'; wrong loc makes remove_response
        # raise "No matching response" and ML falls back to raw counts (bad mag).
        # Pick the most recent epoch.
        pref_chs = sorted(
            (ch for ch in sta.channels if ch.code == pref),
            key=lambda ch: ch.start_date.timestamp if ch.start_date else 0.0,
        )
        default_loc = pref_chs[-1].location_code if pref_chs else ""

        name = sta.site.name if sta.site and sta.site.name else sta.code

        stations.append({
            "net": net.code,
            "sta": sta.code,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "elev": round(elev, 1),
            "name": name,
            "channels": all_ch,
            "default_channel": pref,
            "default_loc": default_loc,
        })

    return stations


def get_stations_multi(
    inventory_paths: list[str] | str,
    lat_min: float | None = None,
    lat_max: float | None = None,
    lon_min: float | None = None,
    lon_max: float | None = None,
) -> list[dict]:
    """Parse SEVERAL inventory XMLs (multi-seedlink sessions) and return the
    merged, (net,sta)-deduplicated station list. Accepts a list of paths or a
    single string with paths separated by ',' / ';' / os.pathsep. Unreadable
    files are skipped so one bad XML does not take the whole session down."""
    if isinstance(inventory_paths, str):
        parts = inventory_paths.replace(";", ",").replace(os.pathsep, ",")
        inventory_paths = [p.strip() for p in parts.split(",")]
    seen: set[tuple[str, str]] = set()
    merged: list[dict] = []
    for path in inventory_paths:
        if not path or not Path(path).exists():
            continue
        try:
            rows = get_stations(path, lat_min, lat_max, lon_min, lon_max)
        except Exception:
            continue
        for r in rows:
            key = (r["net"], r["sta"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(r)
    return merged


def get_palu_sigi_stations(inventory_path: str) -> list[dict]:
    """Shortcut: filter stations in the Palu-Sigi-Donggala-Poso area (Central Sulawesi)."""
    return get_stations(
        inventory_path,
        lat_min=-3.0, lat_max=2.0,
        lon_min=118.0, lon_max=123.5,
    )

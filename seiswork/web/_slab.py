"""
Slab2 auto-generator for SeisWork — by HakimBMKG
================================================

Generates subduction-slab geometry for whatever Area Of Interest (AOI) is active
in the current SeisWork session, using the *official* USGS Slab2.0 models
(Hayes et al., 2018) hosted on ScienceBase.

Pipeline (all region-agnostic, driven by the session AOI bbox):

  1. regions_for_bbox()  -- pick every Slab2 region whose polygon
                            (library/misc/slab_polygons.txt) intersects the AOI.
  2. ensure_dep_grid()   -- download + cache each region's depth grid
                            ([slab]_slab2_dep_[date].grd) from ScienceBase.
  3. generate()          -- clip each grid to the AOI, contour the depth surface
                            with matplotlib, and emit a GeoJSON FeatureCollection
                            of depth contours (LineString, properties.ELEV = depth
                            in km, negative downward) — the SAME schema as the
                            historical footages/Contour_slabs.geojson.

The frontend feeds that FeatureCollection into both the 2-D cross-section views
and the 3-D mesh3d surface, so a single source covers "2D ataupun 3D".

Results are cached per-AOI on disk so opening a result is instant after the first
generation.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
_WEB_DIR    = Path(__file__).resolve().parent
_SLAB2_BASE = _WEB_DIR.parent.parent / "core" / "src" / "slab2"
_POLY_FILE  = _SLAB2_BASE / "slab2code" / "library" / "misc" / "slab_polygons.txt"
_GRID_CACHE = _SLAB2_BASE / "_models_cache"          # downloaded official grids
_RESULT_CACHE = _WEB_DIR / "tmp" / "slab_cache"       # generated GeoJSON per-AOI

# Local slab-guide grids used as offline fallback when ScienceBase is unreachable
_GUIDE_DIR  = _SLAB2_BASE / "slab2code" / "library" / "slabguides"

# ScienceBase parent item that holds all 27 Slab2.0 regional models
_SB_PARENT  = "5aa1b00ee4b0b1c392e86467"
_SB_HOST    = "https://www.sciencebase.gov"

_lock = threading.Lock()
_link_cache: dict | None = None   # {slab_code: sciencebase_item_url}


# ── Region polygons / AOI selection ─────────────────────────────────────────────
def _load_polygons() -> dict[str, list[tuple[float, float]]]:
    """Parse slab_polygons.txt → {region_code: [(lon, lat), ...]}."""
    polys: dict[str, list[tuple[float, float]]] = {}
    if not _POLY_FILE.exists():
        return polys
    for line in _POLY_FILE.read_text().splitlines():
        parts = [p.strip() for p in line.split(",") if p.strip() != ""]
        if len(parts) < 7:
            continue
        code = parts[0].lower()
        try:
            nums = [float(x) for x in parts[1:]]
        except ValueError:
            continue
        pts = list(zip(nums[0::2], nums[1::2]))
        if len(pts) >= 3:
            polys[code] = pts
    return polys


def _norm360(lon: float) -> float:
    """Normalise longitude to [0, 360) so Pacific regions (lon > 180) and
    Indonesian regions compare on the same axis."""
    return lon % 360.0


def _poly_intersects_bbox(poly, bbox, pad=0.0) -> bool:
    """True if a region polygon overlaps the AOI rectangle.

    Uses a cheap bbox-overlap pre-test then a point-in-polygon / corner-in-bbox
    refinement. Longitudes are normalised to [0,360) for the comparison.
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    lon_min -= pad; lat_min -= pad; lon_max += pad; lat_max += pad

    pl = [(_norm360(lo), la) for lo, la in poly]
    plon = [p[0] for p in pl]; plat = [p[1] for p in pl]
    pminx, pmaxx = min(plon), max(plon)
    pminy, pmaxy = min(plat), max(plat)

    blon_min, blon_max = _norm360(lon_min), _norm360(lon_max)
    # Handle a bbox that itself wraps the 0/360 seam by widening conservatively.
    if blon_min > blon_max:
        blon_min, blon_max = 0.0, 360.0

    # 1) bounding-box overlap (necessary condition)
    if pmaxx < blon_min or pminx > blon_max or pmaxy < lat_min or pminy > lat_max:
        return False

    # 2) any AOI corner / centre inside polygon  → definite hit
    corners = [(blon_min, lat_min), (blon_max, lat_min),
               (blon_min, lat_max), (blon_max, lat_max),
               ((blon_min + blon_max) / 2, (lat_min + lat_max) / 2)]
    for cx, cy in corners:
        if _point_in_poly(cx, cy, pl):
            return True

    # 3) any polygon vertex inside AOI → definite hit
    for px, py in pl:
        if blon_min <= px <= blon_max and lat_min <= py <= lat_max:
            return True

    # bbox overlapped but no containment — treat overlap as a hit (clip handles rest)
    return True


def _point_in_poly(x: float, y: float, poly) -> bool:
    """Ray-casting point-in-polygon."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def regions_for_bbox(bbox, pad=0.5) -> list[str]:
    """Return Slab2 region codes whose polygon intersects the AOI bbox."""
    polys = _load_polygons()
    hits = [code for code, poly in polys.items()
            if _poly_intersects_bbox(poly, bbox, pad=pad)]
    # 'exp'/'jap' are synthetic / composite helpers — never real published models.
    return [h for h in hits if h not in ("exp", "jap", "pan")]


# ── ScienceBase grid download (official Slab2.0 models) ─────────────────────────
def _collect_links() -> dict[str, str]:
    """Scrape ScienceBase parent item → {region_code: item_url}. Cached."""
    global _link_cache
    if _link_cache is not None:
        return _link_cache
    import requests
    from bs4 import BeautifulSoup as bs
    rename = {"ala": "alu", "cen": "cam", "new": "png", "sou": "sam", "kam": "kur"}
    out: dict[str, str] = {}
    for off in (0, 20):
        url = f"{_SB_HOST}/catalog/items?parentId={_SB_PARENT}&offset={off}&max=20"
        soup = bs(requests.get(url, timeout=30).content, "html.parser")
        for a in soup.find_all("a"):
            name = str(a.text).lower()
            if "slab2" in name:
                toks = name.split()
                if len(toks) < 9:
                    continue
                code = toks[8][:3]
                code = rename.get(code, code)
                out[code] = _SB_HOST + a["href"]
    _link_cache = out
    return out


def ensure_dep_grid(slab: str) -> Path | None:
    """Return local path to the region's depth grid, downloading from ScienceBase
    (and caching) on first use. Falls back to a local slab-guide grid offline."""
    _GRID_CACHE.mkdir(parents=True, exist_ok=True)
    dest = _GRID_CACHE / f"{slab}_slab2_dep.grd"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    with _lock:
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        try:
            import requests
            from bs4 import BeautifulSoup as bs
            links = _collect_links()
            if slab not in links:
                return _guide_fallback(slab)
            soup = bs(requests.get(links[slab], timeout=30).content, "html.parser")
            spans = soup.find_all("span", {"class": "sb-file-get sb-download-link"})
            dep = None
            for s in spans:
                t = str(s.text)
                if t.lower().endswith(".grd") and "dep" in t.lower():
                    dep = s
                    break
            if dep is None:
                return _guide_fallback(slab)
            durl = _SB_HOST + dep["data-url"]
            r = requests.get(durl, timeout=180)
            r.raise_for_status()
            dest.write_bytes(r.content)
            return dest
        except Exception:
            return _guide_fallback(slab)


def _guide_fallback(slab: str) -> Path | None:
    """Offline fallback: a local slab-guide grid (coarse but real geometry)."""
    for cand in (_GUIDE_DIR / f"{slab}_SG_{slab}.grd",
                 _GUIDE_DIR / "Originals" / f"{slab}.grd"):
        if cand.exists():
            return cand
    return None


# ── Contour generation ──────────────────────────────────────────────────────────
def _decimate(seg, max_pts=400, tol=0.0):
    """Reduce contour vertex count to keep payload small (uniform stride)."""
    n = len(seg)
    if n <= max_pts:
        return seg
    stride = (n + max_pts - 1) // max_pts
    out = seg[::stride]
    if (out[-1] != seg[-1]).any():
        import numpy as np
        out = np.vstack([out, seg[-1]])
    return out


def _contour_region(grid_path: Path, bbox, cint: int) -> list[dict]:
    """Clip one depth grid to the AOI and return GeoJSON contour features."""
    import numpy as np
    import xarray as xr
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    lon_min, lat_min, lon_max, lat_max = bbox
    pad = 0.4
    da = xr.open_dataarray(grid_path)
    # Coordinate names vary (x/y, lon/lat) — normalise.
    xname = "x" if "x" in da.coords else ("lon" if "lon" in da.coords else list(da.coords)[-1])
    yname = "y" if "y" in da.coords else ("lat" if "lat" in da.coords else list(da.coords)[0])
    da = da.sortby(xname).sortby(yname)
    sub = da.sel({xname: slice(lon_min - pad, lon_max + pad),
                  yname: slice(lat_min - pad, lat_max + pad)})
    if sub.size == 0 or min(sub.sizes.values()) < 2:
        return []

    x = sub[xname].values
    y = sub[yname].values
    z = sub.values  # depth, negative km below sea level
    if not np.isfinite(z).any():
        return []

    zmin = float(np.nanmin(z))
    zmax = min(0.0, float(np.nanmax(z)))
    # Contour levels: multiples of cint within the data range (negative km).
    lo = int(np.floor(zmin / cint) * cint)
    hi = int(np.ceil(zmax / cint) * cint)
    levels = list(range(lo, hi + 1, cint))
    levels = [lv for lv in levels if zmin <= lv <= zmax + cint]
    if len(levels) < 2:
        return []

    fig = plt.figure()
    try:
        cs = plt.contour(x, y, z, levels=levels)
        feats: list[dict] = []
        for lev, segs in zip(cs.levels, cs.allsegs):
            for seg in segs:
                if len(seg) < 2:
                    continue
                seg = _decimate(np.asarray(seg))
                coords = [[round(float(px), 6), round(float(py), 6)] for px, py in seg]
                feats.append({
                    "type": "Feature",
                    "properties": {"ELEV": round(float(lev), 1)},
                    "geometry": {"type": "LineString", "coordinates": coords},
                })
        return feats
    finally:
        plt.close(fig)


# ── Top-level generate (with disk cache) ────────────────────────────────────────
def _cache_key(bbox, cint, regions) -> str:
    lon_min, lat_min, lon_max, lat_max = bbox
    rg = "-".join(sorted(regions)) or "none"
    return f"{rg}_{lon_min:.2f}_{lat_min:.2f}_{lon_max:.2f}_{lat_max:.2f}_c{cint}"


def generate(lon_min, lat_min, lon_max, lat_max, cint: int = 25,
             use_cache: bool = True) -> dict:
    """Generate a slab-contour FeatureCollection for the AOI.

    Returns a GeoJSON FeatureCollection (same schema as Contour_slabs.geojson)
    with extra top-level keys: `regions` (codes used) and `cached` (bool).
    """
    bbox = (float(lon_min), float(lat_min), float(lon_max), float(lat_max))
    regions = regions_for_bbox(bbox)

    _RESULT_CACHE.mkdir(parents=True, exist_ok=True)
    key = _cache_key(bbox, cint, regions)
    cpath = _RESULT_CACHE / f"{key}.json"
    if use_cache and cpath.exists():
        try:
            data = json.loads(cpath.read_text())
            data["cached"] = True
            return data
        except Exception:
            pass

    features: list[dict] = []
    used: list[str] = []
    for slab in regions:
        grid = ensure_dep_grid(slab)
        if grid is None:
            continue
        try:
            feats = _contour_region(grid, bbox, cint)
        except Exception:
            feats = []
        if feats:
            for f in feats:
                f["properties"]["region"] = slab
            features.extend(feats)
            used.append(slab)

    fc = {
        "type": "FeatureCollection",
        "name": "Slab2_auto",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "regions": used,
        "cint": cint,
        "features": features,
        "cached": False,
    }
    try:
        cpath.write_text(json.dumps(fc))
    except Exception:
        pass
    return fc

#!/usr/bin/env python3
"""
SeisWork - NonLinLoc travel-time grid generator (global IASP91 model)
Author : HakimBMKG

Builds NLLoc travel-time grids (TIME2D) for any station network at runtime,
using the 1-D IASP91 model with TRANS SIMPLE auto-centered on the network.
Goal: portable NLLoc refinement for any network/region without a pre-built,
station-specific grid.

Pipeline (2 NonLinLoc programs, binaries in core/bin/):
  Vel2Grid  : IASP91 LAYERs -> slowness model grid (P & S)
  Grid2Time : model + each station -> travel-time + angle grid (TIME2D)
  (NLLoc itself is run separately by NLLocLocator using this grid)
"""

from __future__ import annotations
import hashlib
import os
import subprocess

# Simplified 1-D IASP91 model (crust + upper mantle), NLLoc LAYERs:
#   depth_top  Vp Vp_grad  Vs Vs_grad  rho rho_grad
IASP91_LAYERS = [
    (0.0,   5.80, 0.0, 3.36, 0.0, 2.70, 0.0),
    (20.0,  6.50, 0.0, 3.75, 0.0, 2.70, 0.0),
    (35.0,  8.04, 0.0, 4.47, 0.0, 3.30, 0.0),
    (120.0, 8.05, 0.0, 4.50, 0.0, 3.30, 0.0),
    (210.0, 8.30, 0.0, 4.52, 0.0, 3.40, 0.0),
]

# Per-station TIME2D grid (distance-depth), same dims as the bundled grid:
#   201 nodes x 5 km = 1000 km distance; 71 nodes x 5 km, origin -3 km = -3..352 km depth.
# Large enough for regional events of any network.
GRID_NY = 201        # node count along distance
GRID_NZ = 71         # node count along depth
GRID_DXYZ = 5.0      # spacing (km)
GRID_Z0 = -3.0       # depth origin (km, negative = above the surface)
PREFIX = "iasp91"


def auto_grid_dims(stations: list[dict]) -> tuple[int, int, float]:
    """Pick (grid_ny, grid_nz, dxyz) automatically from the network's spread.
    """
    import math
    if len(stations) < 2:
        return GRID_NY, GRID_NZ, GRID_DXYZ

    def _haversine_km(lat1, lon1, lat2, lon2):
        R = 6371.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dphi, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * R * math.asin(math.sqrt(a))

    # Worst-case station-to-epicenter distance ~ network diameter (an event
    # can occur near any station in the footprint). Computed directly rather
    # than 2x radius-from-centroid, to avoid under/over-estimating.
    diameter = 0.0
    for i, a in enumerate(stations):
        for b in stations[i + 1:]:
            dd = _haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            if dd > diameter:
                diameter = dd

    default_range = GRID_NY * GRID_DXYZ
    target_range = diameter * 1.1  # small margin
    if target_range <= default_range:
        return GRID_NY, GRID_NZ, GRID_DXYZ

    dxyz = 10.0 if target_range <= 6500.0 else 20.0
    grid_ny = int(target_range / dxyz) + 20   # +buffer nodes
    grid_nz = int(660.0 / dxyz) + 1           # depth coverage ~660 km (deep subduction zone)
    return grid_ny, grid_nz, dxyz


def station_fingerprint(stations: list[dict]) -> str:
    """Stable hash of the station set (code+lat+lon+elev), used as grid cache key."""
    items = sorted(
        f"{s.get('net','')}.{s['sta']}|{float(s['lat']):.4f}|{float(s['lon']):.4f}|{float(s.get('elev',0) or 0):.1f}"
        for s in stations
    )
    h = hashlib.sha1("\n".join(items).encode()).hexdigest()[:12]
    return h


def _layers_block() -> str:
    return "\n".join(
        f"LAYER {d:.1f} {vp:.2f} {vpg:.2f} {vs:.2f} {vsg:.2f} {rho:.2f} {rhog:.2f}"
        for (d, vp, vpg, vs, vsg, rho, rhog) in IASP91_LAYERS
    )


def _gtsrce_block(stations: list[dict]) -> str:
    # GTSRCE <label> LATLON <lat> <lon> 0.0 <elev_km>. Label must equal the
    # station code used in the OBS file (NLLoc matches prefix.PHASE.<label>.time).
    lines = []
    for s in stations:
        elev_km = (float(s.get("elev", 0) or 0)) / 1000.0
        lines.append(
            f"GTSRCE {s['sta']} LATLON {float(s['lat']):.4f} {float(s['lon']):.4f} 0.0 {elev_km:.3f}"
        )
    return "\n".join(lines)


def _write_ctrl(path: str, phase: str, center: tuple[float, float],
                grid_dir: str, stations: list[dict],
                grid_ny: int = GRID_NY, grid_nz: int = GRID_NZ,
                dxyz: float = GRID_DXYZ) -> None:
    clat, clon = center
    ctrl = f"""# SeisWork NLLoc grid gen — IASP91, TRANS SIMPLE centered on network
CONTROL 1 54321
TRANS SIMPLE {clat:.4f} {clon:.4f} 0.0

VGOUT {os.path.join(grid_dir, PREFIX)}
VGTYPE {phase}
VGGRID 2 {grid_ny} {grid_nz}  0.0 0.0 {GRID_Z0:.1f}  {dxyz:.1f} {dxyz:.1f} {dxyz:.1f}  SLOW_LEN
{_layers_block()}

GTFILES {os.path.join(grid_dir, PREFIX)} {os.path.join(grid_dir, PREFIX)} {phase}
GTMODE GRID2D ANGLES_YES
{_gtsrce_block(stations)}
GT_PLFD 1.0e-3 0
"""
    with open(path, "w") as f:
        f.write(ctrl)


def build_nlloc_grids(stations: list[dict], grid_dir: str,
                      vel2grid_exec: str, grid2time_exec: str,
                      center: tuple[float, float] | None = None,
                      grid_ny: int = GRID_NY, grid_nz: int = GRID_NZ,
                      dxyz: float = GRID_DXYZ,
                      log=lambda *_: None) -> str | None:
    """Generate IASP91 travel-time grids (P & S) for `stations` into `grid_dir`.
    """
    if not stations:
        return None
    if not vel2grid_exec or not grid2time_exec:
        log("Vel2Grid/Grid2Time tak ditemukan — grid global dilewati")
        return None
    if center is None:
        lats = [float(s["lat"]) for s in stations]
        lons = [float(s["lon"]) for s in stations]
        center = (sum(lats) / len(lats), sum(lons) / len(lons))

    os.makedirs(grid_dir, exist_ok=True)
    for phase in ("P", "S"):
        ctrl = os.path.join(grid_dir, f"gen_{phase}.in")
        _write_ctrl(ctrl, phase, center, grid_dir, stations,
                   grid_ny=grid_ny, grid_nz=grid_nz, dxyz=dxyz)
        for exe, tag in ((vel2grid_exec, "Vel2Grid"), (grid2time_exec, "Grid2Time")):
            try:
                r = subprocess.run([exe, ctrl], cwd=grid_dir,
                                   capture_output=True, text=True, timeout=600)
                if r.returncode != 0:
                    log(f"{tag} {phase} gagal (exit {r.returncode}): {r.stdout[-200:]}")
                    return None
            except Exception as exc:   # noqa: BLE001
                log(f"{tag} {phase} error: {exc}")
                return None

    import glob
    if not glob.glob(os.path.join(grid_dir, f"{PREFIX}.P.*.time.hdr")):
        log("grid travel-time tak terbentuk")
        return None
    return grid_dir


def ensure_global_grids(stations: list[dict], cache_root: str,
                        vel2grid_exec: str, grid2time_exec: str,
                        grid_ny: int | None = None, grid_nz: int | None = None,
                        dxyz: float | None = None,
                        center: tuple[float, float] | None = None,
                        log=lambda *_: None) -> tuple[str | None, str]:
    """Cached version: regenerates only when the network fingerprint changes.

    grid_ny/grid_nz/dxyz: None means auto-select via `auto_grid_dims` (fine
    for regional networks, coarse for nationwide ones).
    center: TRANS SIMPLE origin (lat, lon); None means centroid of `stations`.
    When set (e.g. midpoint of a user-drawn area), it's folded into the cache
    key so two grids over the same stations but different centers don't collide.
    Returns (grid_dir | None, prefix), ready to use as grid_dir +
    model_prefix=PREFIX in NLLocLocator.
    """
    fp = station_fingerprint(stations)
    if center is not None:
        # A different TRANS origin must yield a different cache dir, otherwise
        # a region-limited config would silently reuse a centroid-centered grid.
        fp = f"{fp}_{float(center[0]):.3f}_{float(center[1]):.3f}"
    grid_dir = os.path.join(cache_root, fp)
    import glob
    if glob.glob(os.path.join(grid_dir, f"{PREFIX}.P.*.time.hdr")):
        log(f"grid global IASP91 dari cache ({fp}, {len(stations)} stasiun)")
        return grid_dir, PREFIX
    if grid_ny is None or grid_nz is None or dxyz is None:
        grid_ny, grid_nz, dxyz = auto_grid_dims(stations)
    log(f"generate grid global IASP91 utk {len(stations)} stasiun (fp {fp}, "
        f"ny={grid_ny} nz={grid_nz} dxyz={dxyz}km) …")
    out = build_nlloc_grids(stations, grid_dir, vel2grid_exec, grid2time_exec,
                            center=center,
                            grid_ny=grid_ny, grid_nz=grid_nz, dxyz=dxyz, log=log)
    return out, PREFIX

"""
Phase 2 — Real-time PhaseNet picking + association + magnitude — by HakimBMKG

Architecture:
  RealtimePicker    — 1 thread, 1 PhaseNet model (seisbench) loaded ONCE,
                       every cycle loops all active stations & calls
                       model.annotate() per station (looping within 1 process,
                       NOT N separate OS processes — this codebase already
                       demonstrated that process-level parallelism for
                       GPU-bound PhaseNet adds no throughput, only overhead
                       from N×model-load + N×CUDA-context).
  RealtimeAssociator — 1 separate thread, takes the rolling pick buffer →
                       GaMMA (already self-contained, produces its own
                       lat/lon/depth via BGMM+BFGS, does NOT need an NLLoc
                       grid) → when a new event is found, computes ML
                       magnitude from the ring buffer (NOT the built-in
                       MLMagnitude.run() — that fetches from an on-disk SDS
                       which doesn't exist for live-only sessions).

Focal mechanism (FocoNet/SKHASH) is NOT implemented in this slice —
compute_polarities()/FoconetRunner.run() need sds_path (disk), just like
MLMagnitude. Events are stored without a focal mechanism (best-effort).
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import numpy as np

from seiswork.web._seedlink_live import _LIVE_SESSION
from seiswork.utils.channels import BAND_PRIORITY

PICK_WINDOW_S    = 30.0   # window per picking cycle
PICK_OVERLAP_S   = 6.0    # small overlap with the previous cycle (picks near the window edge)
PICK_INTERVAL_S  = 5.0
RESCAN_WINDOW_S  = 180.0  # one-shot rescan window at (re)start — recovers events
                          # missed during restart downtime (last ≈3 minutes)
ASSOC_INTERVAL_S = 15.0
ROLLING_PICKS_MIN = 15.0  # rolling pick buffer used by the associator
MIN_NSTA_EVENT   = 4
DEFAULT_CHANNEL  = f"{BAND_PRIORITY[0]}Z"  # fallback when a station's default_channel is unset (HHZ)
MAX_EVENTS_KEPT  = 50

# ── Duplicate threshold: events are equal if dt < N s AND distance < D km ──
DUP_TIME_S  = 30.0
DUP_DIST_KM = 50.0

# ── Associator backend realtime ──────────────────────────────────────────────
# REAL (travel-time grid, deterministic) is FAR more stable than GaMMA (the GMM
# is refit every cycle → cluster assignments easily flip between cycles). REAL
# associates + gives a rough location; NonLinLoc refines it (iasp91 grid). The realtime
# default = "real". Overridable at start() to "gamma" (the legacy path remains).
ASSOC_BACKEND_DEFAULT = "real"

# ── NLLoc refinement (NonLinLoc refines the GaMMA hypocenter per new event) ──
# GaMMA is only for association + a rough location (BGMM+BFGS); the FINAL location is
# refined by NonLinLoc using the GLOBAL travel-time grid (IASP91). The grid is built once
# (Vel2Grid/Grid2Time) — here it is only referenced; the NLLoc module does not build it.
# Refinement runs PER new EVENT (not every cycle) to keep the loop light;
# if NLLoc fails / the binary is missing → fall back to the GaMMA location (event still kept).
NLLOC_REFINE_ENABLED = True
NLLOC_PROFILE  = "iasp91"   # global grid model_prefix (see *.time.hdr)


def _default_nlloc_grid_dir() -> str:
    """Global IASP91 travel-time grid BUNDLED in the source tree
    (`<repo>/config/nlloc_grids/global`) for portability to other machines —
    no longer depends on the absolute path `~/apps/NLLoc`. Falls back to the old
    location when the bundle is missing (e.g. an older checkout)."""
    bundled = Path(__file__).resolve().parents[2] / "config" / "nlloc_grids" / "global"
    if (bundled).is_dir() and any(bundled.glob(f"{NLLOC_PROFILE}.P.*.time.hdr")):
        return str(bundled)
    # Bundle missing (old checkout) → return "" so NLLoc refinement disables
    # gracefully (instead of pointing at a personal path absent on other machines).
    return ""


NLLOC_GRID_DIR = _default_nlloc_grid_dir()

SDS_RETAIN_DAYS = 1   # work/online_sds default retention (days) — overridable per
                      # config via the "archive_days" field (Edit Configuration in
                      # the GUI). This is the ARCHIVE (on-disk SDS, for historical
                      # event waveform review) — separate from the live ring buffer
                      # in _seedlink_live.py (_BUFFER_SECONDS, in-memory, fixed at
                      # 1h for dashboard responsiveness and not user-configurable).


# ── Slarchive (SDS archive from SeedLink, retained SDS_RETAIN_DAYS days) ────
_SLARCHIVE_PROC: subprocess.Popen | None = None
_SLARCHIVE_LOCK = threading.Lock()


def find_slarchive() -> str:
    """Repo-bundled slarchive first (compiled by install.sh step 3, a known-
    good version independent of any SeisComP install), then whatever's on
    PATH (e.g. the copy SeisComP ships)."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    bundled = os.path.join(repo_root, "core", "bin", "slarchive")
    if os.path.exists(bundled):
        return bundled
    return shutil.which("slarchive") or ""


def online_sds_path(base_dir: str) -> str:
    return str(Path(base_dir) / "work" / "online_sds")


def _seiscomp_sds_is_fresh(sc_sds: str, max_age_hours: float = 2.0) -> bool:
    """True when the SeisComP SDS has a file modified within the last max_age_hours.
    When True → scarchive is active, slarchive isn't needed (avoids duplicate data)."""
    if not sc_sds or not Path(sc_sds).exists():
        return False
    cutoff = time.time() - max_age_hours * 3600
    for f in Path(sc_sds).rglob("*.D"):
        try:
            if f.stat().st_mtime > cutoff:
                return True
        except OSError:
            pass
    return False


def _slarchive_selector(streams) -> str:
    """Build the slarchive -S argument ('NET_STA,NET_STA,…') from a stream list.
    Items can be 'NET.STA.LOC.CHA', 'NET.STA', or a (net, sta) tuple.

    IMPORTANT: without -S, the SeedLink server sends no packets at all → slarchive
    just connects then repeatedly times out after 600s & the SDS stays empty. This is
    why event waveforms sometimes never get archived."""
    if not streams:
        return ""
    seen = []
    for s in streams:
        if isinstance(s, (tuple, list)):
            if len(s) < 2:
                continue
            net, sta = s[0], s[1]
        else:
            parts = str(s).split(".")
            if len(parts) < 2:
                continue
            net, sta = parts[0], parts[1]
        key = f"{net}_{sta}"
        if net and sta and key not in seen:
            seen.append(key)
    return ",".join(seen)


def _kill_stray_slarchive(sds_dir: str) -> int:
    """Kill any ORPHANED slarchive process still writing to the same SDS. When the
    Python (server) process restarts, the global _SLARCHIVE_PROC is lost but the
    child slarchive stays alive → they pile up & fight over the SeedLink connection.
    Scans /proc (Linux); best-effort, safely skipped on OSes without /proc."""
    killed = 0
    try:
        my_pid = os.getpid()
        proc_root = Path("/proc")
        if not proc_root.exists():
            return 0
        for pid_dir in proc_root.iterdir():
            if not pid_dir.name.isdigit():
                continue
            pid = int(pid_dir.name)
            if pid == my_pid:
                continue
            try:
                cmdline = (pid_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
            except OSError:
                continue
            if "slarchive" in cmdline and sds_dir in cmdline:
                try:
                    os.kill(pid, signal.SIGTERM)
                    killed += 1
                except OSError:
                    pass
        if killed:
            engine_log("system", "info",
                       f"slarchive: cleaned up {killed} orphaned process(es) before starting")
    except Exception as exc:
        engine_log("system", "warn", f"failed to clean up orphaned slarchive: {exc}")
    return killed


def start_slarchive(base_dir: str, host: str, port: int,
                    sc_sds: str = "", streams=None,
                    retain_days: int | None = None) -> bool | str:
    """Start slarchive into the local SDS ONLY when the SeisComP SDS is not fresh.
    When SeisComP's own scarchive is active (sc_sds is fresh) → skip (avoid redundancy).
    `streams`: the active station list → becomes the -S selector (REQUIRED for data to flow).
    `retain_days`: archive retention (days), from the config's "archive_days"
    field — defaults to SDS_RETAIN_DAYS when not given.
    Return True = started, False = already running, 'skipped' = SeisComP SDS is fresh."""
    retain_days = int(retain_days) if retain_days else SDS_RETAIN_DAYS
    global _SLARCHIVE_PROC
    with _SLARCHIVE_LOCK:
        if _SLARCHIVE_PROC and _SLARCHIVE_PROC.poll() is None:
            return False   # already running
        # Check whether the SeisComP SDS is still actively fed by scarchive
        if sc_sds and _seiscomp_sds_is_fresh(sc_sds):
            engine_log("system", "info",
                       f"slarchive SKIP — SeisComP SDS is still fresh ({sc_sds}), "
                       f"scarchive is active, no need to duplicate")
            return "skipped"
        sds_dir = Path(online_sds_path(base_dir))
        sds_dir.mkdir(parents=True, exist_ok=True)
        # Clean up orphaned slarchive from earlier restarts/sessions writing to this SDS
        _kill_stray_slarchive(str(sds_dir))
        state_file = str(sds_dir.parent / "slarchive_state.txt")
        log_file   = str(sds_dir.parent / "slarchive.log")
        selector   = _slarchive_selector(streams)
        slarchive_bin = find_slarchive()
        cmd = [
            slarchive_bin or "slarchive", "-v",
            "-SDS", str(sds_dir),
            "-x", state_file,   # save/restore sequence → backfill on restart
            "-Fi",              # skip old records that already exist
            "-nd", "10",        # reconnect delay 10 s
        ]
        if selector:
            cmd += ["-S", selector]   # without this the SDS stays empty (see _slarchive_selector)
        cmd.append(f"{host}:{port}")
        # Built-in fallback: if slarchive isn't bundled or on PATH, write the SDS via ObsPy
        if not slarchive_bin:
            _LIVE_SESSION.set_sds_path(str(sds_dir))
            engine_log("system", "info",
                       f"slarchive not found (bundled or on PATH) — "
                       f"using the built-in ObsPy SDS → {sds_dir}")
            threading.Thread(target=_sds_cleanup_loop,
                             args=(str(sds_dir), retain_days),
                             daemon=True, name="sds-cleanup").start()
            return "builtin"
        try:
            with open(log_file, "a") as lf:
                # start_new_session=True → slarchive gets its own session, not a direct
                # Flask child. When Flask restarts (HUP), slarchive does not
                # turn into a zombie because it is not orphaned from the same parent.
                _SLARCHIVE_PROC = subprocess.Popen(
                    cmd, stdout=lf, stderr=lf, close_fds=True,
                    start_new_session=True)
            n_sta = selector.count(",") + 1 if selector else 0
            engine_log("system", "info",
                       f"slarchive START → SDS {sds_dir} "
                       f"({n_sta} stations via -S, backfill via -x statefile)"
                       if selector else
                       f"slarchive START → SDS {sds_dir} (WITHOUT a stream selector — "
                       f"data may not come in!)")
            threading.Thread(target=_sds_cleanup_loop,
                             args=(str(sds_dir), retain_days),
                             daemon=True, name="sds-cleanup").start()
            return True
        except Exception as exc:
            engine_log("system", "warn", f"slarchive failed to start: {exc}")
            return False


def stop_slarchive():
    global _SLARCHIVE_PROC
    with _SLARCHIVE_LOCK:
        if _SLARCHIVE_PROC is None:
            return
        if _SLARCHIVE_PROC.poll() is None:
            _SLARCHIVE_PROC.terminate()
            try:
                _SLARCHIVE_PROC.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _SLARCHIVE_PROC.kill()
        _SLARCHIVE_PROC = None
    engine_log("system", "info", "slarchive STOP")


def slarchive_running() -> bool:
    with _SLARCHIVE_LOCK:
        proc_ok = _SLARCHIVE_PROC is not None and _SLARCHIVE_PROC.poll() is None
    return proc_ok or bool(_LIVE_SESSION._sds_path)


def _sds_cleanup_loop(sds_dir: str, max_days: int = SDS_RETAIN_DAYS):
    """Delete SDS files older than max_days, once per hour — so recent event
    waveforms stay openable while storage doesn't fill up.
    Filename format: NET.STA.LOC.CHA.D.YYYY.DDD"""
    while True:
        time.sleep(3600)
        try:
            cutoff_day = (datetime.now(timezone.utc) - timedelta(days=max_days))
            for f in Path(sds_dir).rglob("*.D"):
                parts = f.name.rsplit(".", 2)   # NET.STA..CHA.D | YYYY | DDD
                if len(parts) == 3:
                    try:
                        year, jday = int(parts[1]), int(parts[2])
                        fdate = datetime(year, 1, 1, tzinfo=timezone.utc) + \
                                timedelta(days=jday - 1)
                        if fdate < cutoff_day:
                            f.unlink()
                            engine_log("system", "info",
                                       f"SDS cleanup: removed {f.name}")
                    except (ValueError, OSError):
                        pass
        except Exception:
            pass


# ── Indonesian cities for nearest-distance calculation ──────────────────────
# Extra list (important towns/districts missing from the regency/city data):
_CITIES_EXTRA: dict[str, tuple[float, float]] = {
    "Jailolo"     : ( 1.089,  127.500),
    "Tobelo"      : ( 1.726,  128.002),
    "Sofifi"      : ( 0.729,  127.568),
    "Labuha"      : (-0.631,  127.491),
    "Sanana"      : (-2.057,  125.986),
    "Morotai"     : ( 2.322,  128.268),
    "Ampana"      : (-0.871,  121.588),
    "Masohi"      : (-3.338,  128.924),
    "Saumlaki"    : (-7.985,  131.303),
    "Dobo"        : (-5.773,  134.213),
    "Poso"        : (-1.397,  120.762),
    "Palopo"      : (-2.993,  120.196),
    "Luwuk"       : (-0.940,  122.790),
    "Amurang"     : ( 1.184,  124.581),
    "Tahuna"      : ( 3.623,  125.488),
    "Tomohon"     : ( 1.321,  124.832),
    "Wamena"      : (-4.090,  138.955),
    "Biak"        : (-1.176,  136.081),
    "Nabire"      : (-3.362,  135.497),
    "Timika"      : (-4.530,  136.887),
    "Ruteng"      : (-8.613,  120.469),
    "Maumere"     : (-8.624,  122.213),
    "Ende"        : (-8.835,  121.657),
    "Labuan Bajo" : (-8.483,  119.889),
    "Waingapu"    : (-9.656,  120.258),
    "Batam"       : ( 1.045,  104.030),
    "Tarakan"     : ( 3.317,  117.636),
    "Banda Aceh"  : ( 5.549,   95.323),
    "Solo"        : (-7.566,  110.825),
    "Yogyakarta"  : (-7.795,  110.369),
}

_CITIES_ID: dict[str, tuple[float, float]] = {}  # filled on the first _load_cities() call
_CITIES_LOADED = False
_CITIES_LOCK = threading.Lock()


def _load_cities() -> None:
    """Load the regency/city list from indonesia-region.min.json + _CITIES_EXTRA.
    Regency/city level only (not district) — 514 locations + 30 extras = ~520 total."""
    global _CITIES_ID, _CITIES_LOADED
    if _CITIES_LOADED:
        return
    with _CITIES_LOCK:
        if _CITIES_LOADED:
            return
        merged: dict[str, tuple[float, float]] = {}
        try:
            _fp = Path(__file__).resolve().parent / "footages" / "indonesia-region.min.json"
            if _fp.exists():
                import json as _json
                with _fp.open() as _f:
                    _data = _json.load(_f)
                for _prov in _data:
                    for _reg in _prov.get("regencies", []):
                        _rname = (_reg.get("name", "")
                                  .replace("KABUPATEN ", "").replace("KOTA ", "").title())
                        _rlat, _rlon = _reg.get("latitude"), _reg.get("longitude")
                        if _rname and _rlat and _rlon:
                            merged[_rname] = (float(_rlat), float(_rlon))
        except Exception:
            pass
        merged.update(_CITIES_EXTRA)
        _CITIES_ID = merged
        _CITIES_LOADED = True

# Compass directions in Indonesian (16 points)
_DIR_ID = [
    "U", "UTL", "TL", "TTL",
    "T", "TTG", "TG", "STG",
    "S", "SBD", "BD", "BBD",
    "B", "BBL", "BL", "UBL",
]


def _bearing_dir(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """Direction from the earthquake to the city, as an Indonesian abbreviation (8 directions)."""
    from math import atan2, degrees
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    x = sin(d_lon) * cos(radians(lat2))
    y = cos(radians(lat1)) * sin(radians(lat2)) - \
        sin(radians(lat1)) * cos(radians(lat2)) * cos(d_lon)
    bearing = (degrees(atan2(x, y)) + 360) % 360
    # 8 directions: every 45°
    dirs8 = ["U", "TL", "T", "TG", "S", "BD", "B", "BL"]
    idx = int((bearing + 22.5) / 45) % 8
    return dirs8[idx]


def nearest_city_info(lat: float, lon: float) -> dict:
    """Return {city, dist_km, direction} — the nearest Indonesian city/regency to the quake coordinates.
    direction = direction from the QUAKE to the CITY (e.g. 'TL' → the quake is SW of the city)."""
    _load_cities()
    best_name, best_dist = "", float("inf")
    for name, (clat, clon) in _CITIES_ID.items():
        d = _haversine_km(lat, lon, clat, clon)
        if d < best_dist:
            best_dist = d
            best_name = name
    if not best_name:
        return {"city": "—", "dist_km": None, "direction": ""}
    clat, clon = _CITIES_ID[best_name]
    direction = _bearing_dir(lat, lon, clat, clon)
    return {
        "city"     : best_name,
        "dist_km"  : round(best_dist, 1),
        "direction": direction,
    }


# ── FocoNet focal mechanism on-demand (1 event realtime) ─────────────────────
def compute_event_focal(event: dict, picks_p_rows: list, sds_path: str,
                        inventory_path: str, network: str, channel: str,
                        base_dir: str, out_dir: str) -> tuple:
    """Best-effort FocoNet_O focal mechanism for ONE realtime event.

    picks_p_rows: list of {"network","station","pick_time"} (UTCDateTime/str) —
    P picks ONLY. First-motion polarities are read from SDS (needs the archived
    waveform to cover the pick time). Return (focal_dict | None, status_msg).
    focal_dict = {strike,dip,rake,n_pol,method}. A beachball PNG is written to
    out_dir/<event_id>.png (used by the frontend as an image)."""
    import pandas as _pd, os as _os
    from seiswork.modules.mechanism.polarity import compute_polarities
    from seiswork.modules.mechanism.foconet_runner import FocoNetRunner

    eid = str(event["event_id"])
    if len(picks_p_rows) < 3:
        return None, f"Too few P picks ({len(picks_p_rows)}, need ≥3)"
    if not sds_path or not Path(sds_path).exists():
        return None, "SDS waveform unavailable for computing polarities"

    # pick_time may be a UTCDateTime, datetime, or string.
    # polarity.py uses pd.Timestamp(t_pick) → needs an ISO string or datetime,
    # NOT a raw UTCDateTime (pd.Timestamp(UTCDateTime) raises TypeError).
    def _to_iso(t):
        if hasattr(t, "isoformat"):
            return t.isoformat()          # datetime / UTCDateTime
        return str(t)

    picks_p = _pd.DataFrame([
        {"event_id": eid, "network": p["network"], "station": p["station"],
         "pick_time": _to_iso(p["pick_time"])}
        for p in picks_p_rows
    ])
    # 1) First-motion polarities from SDS (NaN when the waveform is unreadable)
    pol_df = compute_polarities(picks_p, sds_path, channel=channel)
    n_ok = int(pol_df["p_polarity"].notna().sum())
    if n_ok < 3:
        return None, (f"polarities read for {n_ok} stations (need ≥3) — "
                      f"the waveform may not be archived in SDS yet")

    # 2) One-row catalog (event_id MUST match pol_df for the tensor to line up)
    _os.makedirs(out_dir, exist_ok=True)
    cat = _pd.DataFrame([{
        "event_id": eid, "datetime": event["datetime"],
        "lat": event["lat"], "lon": event["lon"],
        "depth_km": event["depth_km"], "mag": event.get("mag") or 0,
    }])
    cat_csv = _os.path.join(out_dir, f"_focal_cat_{eid}.csv")
    cat.to_csv(cat_csv, index=False)

    # 3) FocoNet_O (polarity-only) — model di core/src/FocoNet
    fn = FocoNetRunner({"mechanism": {"foconet": {"device": "cpu"}}}, base_dir)
    fn.out_dir = out_dir
    res = fn.run(catalog_csv=cat_csv, picks_csv="", sds_path=sds_path,
                 inventory_path=inventory_path, network=network, channel=channel,
                 pol_df=pol_df, picks_p_df=picks_p, picks_s_df=None)
    try:
        _os.remove(cat_csv)
    except OSError:
        pass
    if res is None or len(res) == 0:
        return None, "FocoNet produced no solution (n_pol<1)"
    r = res.iloc[0]
    return ({"strike": float(r["strike"]), "dip": float(r["dip"]),
             "rake": float(r["rake"]), "n_pol": int(r["n_pol"]),
             "method": str(r["method"])}, "ok")


# ── Duplikat spasio-temporal ─────────────────────────────────────────────────
def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    dla, dlo = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dla / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlo / 2) ** 2
    return 2 * r * asin(min(1.0, sqrt(a)))


def _event_epoch(ev: dict) -> float | None:
    try:
        return datetime.fromisoformat(
            str(ev["datetime"]).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def is_duplicate_event(new_ev: dict, existing: list[dict],
                       time_s: float = DUP_TIME_S,
                       dist_km: float = DUP_DIST_KM) -> bool:
    """True when new_ev is too close (time + space) to one of the existing events."""
    t_new = _event_epoch(new_ev)
    if t_new is None:
        return False
    for ev in existing:
        t_ev = _event_epoch(ev)
        if t_ev is None:
            continue
        if abs(t_new - t_ev) > time_s:
            continue
        if _haversine_km(new_ev["lat"], new_ev["lon"],
                         ev["lat"], ev["lon"]) <= dist_km:
            return True
    return False


def dedup_catalog(base_dir: str,
                  time_s: float = DUP_TIME_S,
                  dist_km: float = DUP_DIST_KM,
                  cfg_id: str | None = None) -> int:
    """Remove duplicates from the on-disk JSONL catalog. Returns the number removed.
    Keeps the event with the largest nsta; ties keep whichever was detected first."""
    path = _catalog_path(base_dir, cfg_id)
    if not path.exists():
        return 0
    with _CATALOG_LOCK, path.open() as f:
        lines = [l.strip() for l in f if l.strip()]
    events = []
    for ln in lines:
        try:
            events.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    kept, removed = [], 0
    for ev in events:
        if is_duplicate_event(ev, kept, time_s, dist_km):
            removed += 1
        else:
            kept.append(ev)
    if removed:
        with _CATALOG_LOCK, path.open("w") as f:
            for ev in kept:
                f.write(json.dumps(ev, default=str) + "\n")
    return removed


# ── Engine log terpadu ──────────────────────────────────────────────────────
# Satu aliran kronologis aktivitas SEMUA komponen real-time: PhaseNet (picker),
# GaMMA (associator), and scautopick (bridge). Used by the UI to prove each
# component actually RUNS (not just a counter). Module-level ring buffer so the
# picker & associator (2 different threads) write to the same log.
_ENGINE_LOG: deque = deque(maxlen=300)
_ENGINE_LOG_LOCK = threading.Lock()


def engine_log(source: str, level: str, msg: str):
    """source ∈ {denoiser, phasenet, gamma, scautopick, system}; level ∈
    {info, event, warn, error} (used by the UI for coloring). Thread-safe."""
    with _ENGINE_LOG_LOCK:
        _ENGINE_LOG.append({
            "t": time.time(), "source": source, "level": level, "msg": msg,
        })


def get_engine_log(n: int = 80) -> list[dict]:
    with _ENGINE_LOG_LOCK:
        items = list(_ENGINE_LOG)
    return items[-n:] if len(items) > n else items


def clear_engine_log():
    with _ENGINE_LOG_LOCK:
        _ENGINE_LOG.clear()


# ── Persistent event catalog ────────────────────────────────────────────────
# Detected+computed events are saved to disk (append-only JSONL) forming a
# CATALOG that survives server/detection restarts. The in-memory
# `RealtimeAssociator.events` resets on every start(), but the disk catalog does not —
# on start() the catalog is loaded back into memory so the event list is not lost.
_CATALOG_LOCK = threading.Lock()


# ── Active online cfg_id (per-config catalog) ─────────────────────────────────
_ACTIVE_CFG_ID: str | None = None
_CFG_ID_FILE   = "work/online_catalog/.active_cfg_id"


def set_active_cfg_id(base_dir: str, cfg_id: str | None):
    global _ACTIVE_CFG_ID
    _ACTIVE_CFG_ID = cfg_id or None
    try:
        f = Path(base_dir) / _CFG_ID_FILE
        f.parent.mkdir(parents=True, exist_ok=True)
        if cfg_id:
            f.write_text(cfg_id)
        else:
            f.unlink(missing_ok=True)
    except Exception:
        pass


def get_active_cfg_id(base_dir: str) -> str | None:
    if _ACTIVE_CFG_ID:
        return _ACTIVE_CFG_ID
    try:
        v = (Path(base_dir) / _CFG_ID_FILE).read_text().strip()
        return v or None
    except Exception:
        return None


def _catalog_path(base_dir: str, cfg_id: str | None = None) -> Path:
    cid = cfg_id or _ACTIVE_CFG_ID or get_active_cfg_id(base_dir)
    p = Path(base_dir) / "work" / "online_catalog"
    if cid:
        p = p / cid
    p.mkdir(parents=True, exist_ok=True)
    return p / "events.jsonl"


def _event_networks(sta_list) -> list[str]:
    """Unique network codes from the stations that RECORDED the event (NET.STA key) —
    distinguishes the event's source/network (e.g. AM=RaspberryShake/raspy vs IA/AF=Palu)
    so events from different configs can be told apart even in a single catalog file. Sorted."""
    nets = set()
    for s in (sta_list or []):
        code = str(s).split(".")[0].strip()
        if code:
            nets.add(code)
    return sorted(nets)


def append_catalog(base_dir: str, event: dict, cfg_id: str | None = None):
    """Add 1 event to the disk catalog (append JSONL). Thread-safe, best-effort."""
    if not base_dir:
        return
    try:
        with _CATALOG_LOCK, _catalog_path(base_dir, cfg_id).open("a") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception as exc:   # noqa: BLE001 — a catalog write failure must not drop the event
        engine_log("system", "warn", f"catalog: failed to save the event — {exc}")


def update_catalog_focal(base_dir: str, event_id: str, focal: dict,
                         cfg_id: str | None = None):
    """Tulis ulang field 'focal' utk 1 event di katalog JSONL (best-effort)."""
    if not base_dir:
        return
    path = _catalog_path(base_dir, cfg_id)
    if not path.exists():
        return
    with _CATALOG_LOCK:
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        out = []
        for ln in lines:
            try:
                ev = json.loads(ln)
            except json.JSONDecodeError:
                out.append(ln); continue
            if ev.get("event_id") == event_id:
                ev["focal"] = focal
                out.append(json.dumps(ev, default=str))
            else:
                out.append(ln)
        path.write_text("\n".join(out) + "\n")


def load_catalog(base_dir: str, limit: int = 0,
                 cfg_id: str | None = None) -> list[dict]:
    """Read the disk catalog → event list NEWEST FIRST (index 0 = most recent).
    limit>0 caps the number of events returned."""
    if not base_dir:
        return []
    path = _catalog_path(base_dir, cfg_id)
    if not path.exists():
        return []
    out = []
    try:
        with _CATALOG_LOCK, path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    out.reverse()   # newest first
    # Backfill nearest_city for old events (JSONL rows lacking this field)
    for ev in out:
        if "nearest_city" not in ev and ev.get("lat") is not None and ev.get("lon") is not None:
            try:
                ev["nearest_city"] = nearest_city_info(float(ev["lat"]), float(ev["lon"]))
            except Exception:
                pass
        # Backfill the network discriminator for old events (from recording stations).
        if "networks" not in ev and ev.get("stations"):
            ev["networks"] = _event_networks(ev.get("stations"))
    return out[:limit] if limit > 0 else out


def _fmt_t(epoch: float) -> str:
    return datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _parse_t(s: str) -> float:
    # phase_time is always UTC (built from a starttime UTCDateTime). It MUST be parsed
    # as UTC — datetime.strptime(...).timestamp() interprets naive datetimes as
    # LOCAL time (e.g. WIB +7) → epoch off by 7 hours → picks fall outside the window
    # and never plot on the waveform. replace(tzinfo=utc) forces UTC interpretation.
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").replace(
        tzinfo=timezone.utc).timestamp()


class RealtimePicker:
    """1 PhaseNet model loaded once; every cycle picks all active stations."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._station_channels: dict = {}   # {(net,sta): cha}
        self._base_dir = ""
        self._helper = None   # PhaseNetPicker instance (only used by _filter/_extract_picks)
        self.model = None
        self._denoise = False
        self._denoise_pretrained = "original"
        self.denoiser_model = None   # SeisBench DeepDenoiser (loaded on demand)
        self.n_cycles = 0
        self.n_picks_total = 0
        self.last_cycle_time = None
        self.error = None
        self.rolling_picks: list[dict] = []   # consumed by RealtimeAssociator

    def start(self, base_dir: str, station_channels: dict,
              p_threshold: float = 0.3, s_threshold: float = 0.6,
              denoise: bool = False, denoise_pretrained: str = "original") -> bool:
        with self._lock:
            if self._running:
                return False
            self._base_dir = base_dir
            self._station_channels = dict(station_channels)
            self._p_threshold = float(p_threshold)
            self._s_threshold = float(s_threshold)
            self._denoise = bool(denoise)
            self._denoise_pretrained = str(denoise_pretrained) or "original"
            self._running = True
            self.error = None
            self.n_cycles = 0
            self.n_picks_total = 0
            self.rolling_picks = []
            self._helper = None   # force model reload with new thresholds
            self.model = None
            self.denoiser_model = None
        clear_engine_log()
        _dd_tag = f" + DeepDenoiser({denoise_pretrained})" if denoise else ""
        engine_log("phasenet", "info",
                   f"PhaseNet picker START — {len(station_channels)} active stations "
                   f"(P≥{p_threshold:.2f} S≥{s_threshold:.2f}){_dd_tag}")
        if denoise:
            engine_log("denoiser", "info",
                       f"DeepDenoiser enabled (pretrained='{denoise_pretrained}') — "
                       f"will process waveforms before PhaseNet")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        with self._lock:
            self._running = False
        engine_log("phasenet", "info", "PhaseNet picker STOP")

    def status(self) -> dict:
        return {
            "running"           : self._running,
            "n_stations"        : len(self._station_channels),
            "n_cycles"          : self.n_cycles,
            "n_picks_total"     : self.n_picks_total,
            "last_cycle_time"   : self.last_cycle_time,
            "error"             : self.error,
            "denoise"           : bool(getattr(self, "_denoise", False)),
            "denoise_pretrained": str(getattr(self, "_denoise_pretrained", "original")),
            "denoiser_ready"    : self.denoiser_model is not None,
        }

    def _ensure_model(self):
        if self._helper is not None:
            return
        from seiswork.modules.picker.phasenet import PhaseNetPicker
        # Threshold P < S is INTENTIONAL: the live picker feeds PhaseNet only the
        # VERTICAL (Z) component — many IA stations are 1-component short-period (SHZ),
        # no horizontals. PhaseNet is a 3-component (ZNE) model; on Z-only input the S head
        # (which relies on horizontal energy) is out-of-distribution → often picks
        # FALSE S. P is reliable on the vertical. So: P more sensitive (0.3), S stricter
        # (0.6) to suppress false S & highlight P. (If a 3-component feed arrives later,
        # rebalance.)
        p_thr = getattr(self, "_p_threshold", 0.3)
        s_thr = getattr(self, "_s_threshold", 0.6)
        cfg = {
            "pick": {"phasenet": {
                "model": "PhaseNet", "pretrained": "stead",
                "batch_size": 16, "highpass_hz": 1.0,
                "p_threshold": p_thr, "s_threshold": s_thr,
            }},
            "data": {"waveform_dir": "work/_realtime_dummy"},
            "fdsn": {},
        }
        engine_log("phasenet", "info", "loading the PhaseNet model (seisbench/STEAD)…")
        helper = PhaseNetPicker(cfg, self._base_dir)
        self.model = helper._load_model()
        self._helper = helper
        try:
            import torch as _torch
            dev = "GPU/CUDA" if _torch.cuda.is_available() else "CPU"
        except Exception:
            dev = "?"
        engine_log("phasenet", "info", f"PhaseNet model ready (device: {dev})")

        # Load DeepDenoiser model if requested
        if getattr(self, "_denoise", False):
            self._ensure_denoiser()

    def _ensure_denoiser(self):
        """Load (or reuse) the SeisBench DeepDenoiser model."""
        if self.denoiser_model is not None:
            return
        pretrained = getattr(self, "_denoise_pretrained", "original")
        for attempt in (1, 2):
            try:
                import seisbench.models as sbm
                import torch as _torch
                engine_log("denoiser", "info",
                           f"loading DeepDenoiser pretrained='{pretrained}'…")
                dm = sbm.DeepDenoiser.from_pretrained(pretrained)
                if _torch.cuda.is_available():
                    dm = dm.cuda()
                dm.eval()
                self.denoiser_model = dm
                dev_dd = "GPU/CUDA" if _torch.cuda.is_available() else "CPU"
                engine_log("denoiser", "info",
                           f"DeepDenoiser READY — pretrained='{pretrained}' device={dev_dd} "
                           f"(pre-processing active before PhaseNet)")
                return
            except Exception as exc:
                # A download interrupted mid-transfer (network drop, killed process) leaves
                # "*.partial" files in the SeisBench cache — the packaging library then
                # chokes on that half-written filename ("Invalid version: '1.partial'") on
                # every subsequent load, permanently disabling the denoiser until someone
                # notices and clears the cache by hand. Detect that case, remove the stale
                # partials, and retry the download once before giving up.
                if attempt == 1 and self._clear_stale_denoiser_cache():
                    engine_log("denoiser", "warn",
                               f"stale partial download found in the SeisBench cache — retrying ({exc})")
                    continue
                engine_log("denoiser", "error",
                           f"DeepDenoiser failed to load ({exc}), continuing without denoising")
                self.denoiser_model = None
                self._denoise = False
                return

    @staticmethod
    def _clear_stale_denoiser_cache() -> bool:
        """Remove leftover '*.partial' files from an interrupted SeisBench download."""
        try:
            import seisbench
            cache_dir = Path(seisbench.cache_root) / "models" / "v3" / "deepdenoiser"
            partials = list(cache_dir.glob("*.partial"))
            for p in partials:
                p.unlink(missing_ok=True)
            return bool(partials)
        except Exception:
            return False

    def _denoise_stream(self, st):
        """Terapkan DeepDenoiser ke stream; kembalikan stream bersih atau original jika gagal.

        Called from _run_cycle() with a COMBINED multi-station stream (see Phase 2
        there) — batch_size must be large enough to actually batch across stations'
        windows in one GPU pass, or denoising still runs ~one-window-at-a-time
        internally and the whole point of combining stations upstream is lost.
        """
        if not getattr(self, "_denoise", False) or self.denoiser_model is None:
            return st
        try:
            import torch as _torch
            from obspy import Stream
            with _torch.no_grad():
                st_ann = self.denoiser_model.annotate(st, batch_size=64)
            signal_traces = []
            for tr in st_ann:
                if "__DeepDenoiser_signal" in tr.stats.channel:
                    tr_out = tr.copy()
                    tr_out.stats.channel = tr.stats.channel.split("__")[0]
                    signal_traces.append(tr_out)
            if signal_traces:
                return Stream(signal_traces)
        except Exception as exc:
            engine_log("denoiser", "warn", f"denoise failed: {exc}")
        return st

    def ensure_denoiser_async(self) -> bool:
        """For the GUI 'denoise view' toggle: load DeepDenoiser in the background
        so the web request never blocks on the (possibly downloading) model.
        Returns True once the model is ready, False while still loading."""
        if self.denoiser_model is not None:
            return True
        if getattr(self, "_denoiser_loading", False):
            return False
        self._denoiser_loading = True

        def _load():
            try:
                self._ensure_denoiser()
            finally:
                self._denoiser_loading = False

        threading.Thread(target=_load, daemon=True).start()
        return False

    def denoise_for_view(self, st):
        """Denoise a stream for DISPLAY only — independent of the picking
        `_denoise` flag (which gates _denoise_stream). Returns a denoised Stream,
        or None if the model is not ready / denoising failed."""
        if self.denoiser_model is None:
            return None
        try:
            import torch as _torch
            from obspy import Stream
            with _BACKFILL_MODEL_LOCK:
                with _torch.no_grad():
                    st_ann = self.denoiser_model.annotate(st, batch_size=1)
            signal_traces = []
            for tr in st_ann:
                if "__DeepDenoiser_signal" in tr.stats.channel:
                    tr_out = tr.copy()
                    tr_out.stats.channel = tr.stats.channel.split("__")[0]
                    signal_traces.append(tr_out)
            return Stream(signal_traces) if signal_traces else None
        except Exception as exc:
            engine_log("denoiser", "warn", f"view-denoise failed: {exc}")
            return None

    def _loop(self):
        try:
            self._ensure_model()
        except Exception as exc:
            self.error = f"model load failed: {exc}"
            engine_log("phasenet", "error", f"FAILED to load model: {exc}")
            with self._lock:
                self._running = False
            return

        import torch
        from obspy import Stream, Trace, UTCDateTime

        # ── One-shot rescan at (re)start ─────────────────────────────────────
        # A server restart introduces downtime that can miss events.
        # _LIVE_SESSION.start() already backfills the latest window into the ring buffer;
        # here the picker scans the ENTIRE last RESCAN_WINDOW_S (≈3 minutes) ONCE
        # so picks from missed events enter rolling_picks → the associator
        # forms their events. Wait briefly so the backfill can fill the buffer.
        try:
            self._startup_rescan(torch, Stream, Trace, UTCDateTime)
        except Exception as exc:   # a failed rescan must not stop the live picker
            engine_log("phasenet", "warn", f"restart rescan failed: {exc}")

        while True:
            with self._lock:
                if not self._running:
                    break
            t_cycle0 = time.time()
            try:
                n_new = self._run_cycle(torch, Stream, Trace, UTCDateTime)
                self.n_cycles += 1
                self.n_picks_total += n_new
                self.last_cycle_time = time.time()
                self.error = None
            except Exception as exc:   # noqa: BLE001 — never let the thread die silently
                self.error = str(exc)

            elapsed = time.time() - t_cycle0
            time.sleep(max(0.5, PICK_INTERVAL_S - elapsed))

    def _run_cycle(self, torch, Stream, Trace, UTCDateTime,
                   window_s: float | None = None) -> int:
        n_new = 0
        n_p = 0
        n_s = 0
        n_sta_data = 0   # stations with enough data to run inference on
        cutoff = time.time() - ROLLING_PICKS_MIN * 60
        win = window_s if (window_s and window_s > 0) else (PICK_WINDOW_S + PICK_OVERLAP_S)

        # ── Phase 1: gather every station's raw trace (cheap, no model calls yet) ──
        station_items = list(self._station_channels.items())
        sta_data = []
        for (net, sta), cha in station_items:
            t, v = _LIVE_SESSION.get_window(net, sta, "", cha, seconds=win)
            sr = _LIVE_SESSION.get_sampling_rate(net, sta, "", cha)
            if len(t) < 50 or not sr:
                continue
            n_sta_data += 1

            tr = Trace(data=v.astype(np.float32))
            tr.stats.network = net
            tr.stats.station = sta
            tr.stats.channel = cha
            tr.stats.sampling_rate = float(sr)
            tr.stats.starttime = UTCDateTime(float(t[0]))
            sta_data.append({"net": net, "sta": sta, "cha": cha,
                              "tr": tr, "raw": Stream([tr.copy()])})

        if not sta_data:
            if self.n_cycles % 6 == 0:
                engine_log("phasenet", "info", "scanned 0 stations → 0 new picks; rolling=0")
            return 0

        # ── Phase 2: ONE batched model call for ALL stations at once (DeepDenoiser
        # then PhaseNet), instead of looping station-by-station. SeisBench's
        # annotate() groups a multi-station Stream internally by NET.STA and batches
        # the GPU forward pass across the whole call — verified empirically to give
        # bit-identical output (~1e-15 float noise) to calling annotate() once per
        # station, just far fewer/larger model calls. This is what removed the need
        # for the earlier per-cycle denoise-rotation workaround (DENOISE_MAX_STATIONS_
        # PER_CYCLE): denoising every station every cycle is now cheap enough that a
        # single station's added compute no longer risks starving other Flask
        # requests (the "Waveform timeout" symptom from before).
        ann_combined = None
        try:
            with _BACKFILL_MODEL_LOCK:
                combined = Stream([d["tr"].copy() for d in sta_data])
                combined = self._helper._filter(combined)
                if getattr(self, "_denoise", False) and self.denoiser_model is not None:
                    combined = self._denoise_stream(combined)   # logs its own failures ("denoiser")
                with torch.no_grad():
                    ann_combined = self.model.annotate(combined, batch_size=64)
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception as exc:
            engine_log("phasenet", "warn", f"batch annotate failed ({n_sta_data} stations) — {exc}")

        if ann_combined is None or len(ann_combined) == 0:
            with self._lock:
                self.rolling_picks = [p for p in self.rolling_picks
                                       if _parse_t(p["phase_time"]) >= cutoff]
                n_roll = len(self.rolling_picks)
            if self.n_cycles % 6 == 0:
                engine_log("phasenet", "info",
                           f"scanned {n_sta_data} stations → 0 new picks; rolling={n_roll}")
            return 0

        # ── Phase 3: split the combined annotation back out per station (cheap CPU
        # peak-finding, _measure_amp only looks at the given raw_stream's own trace —
        # MUST stay isolated per station here, unlike the model calls above, or
        # amplitude/picks would get attributed to the wrong station). ──────────────
        for d in sta_data:
            net, sta = d["net"], d["sta"]
            ann_sta = ann_combined.select(network=net, station=sta)
            if not ann_sta:
                continue

            key = f"{net}.{sta}"
            df = self._helper._extract_picks(key, ann_sta, d["raw"])
            if df.empty:
                continue

            for _, row in df.iterrows():
                t_pick = _parse_t(row["phase_time"])
                _LIVE_SESSION.add_pick(net, sta, t_pick, row["phase_hint"], source="phasenet")
                with self._lock:
                    self.rolling_picks.append({
                        "network"    : net, "station": sta, "location": "",
                        "channel"    : row["channel"], "phase_hint": row["phase_hint"],
                        "phase_time" : row["phase_time"],
                        "phase_score": float(row["phase_score"]),
                        "phase_amp"  : row["phase_amp"],
                    })
                if str(row["phase_hint"]).upper().startswith("P"):
                    n_p += 1
                else:
                    n_s += 1
                n_new += 1

        with self._lock:
            self.rolling_picks = [p for p in self.rolling_picks
                                   if _parse_t(p["phase_time"]) >= cutoff]
            n_roll = len(self.rolling_picks)
        # Per-cycle log: proof PhaseNet is running + a result summary. Heartbeat every
        # ~6 cycles (≈30 s) even with 0 picks, so the log stays alive without flooding.
        if n_new > 0:
            engine_log("phasenet", "info",
                       f"scanned {n_sta_data} stations → +{n_new} picks (P{n_p}/S{n_s}); rolling={n_roll}")
        elif self.n_cycles % 6 == 0:
            engine_log("phasenet", "info",
                       f"scanned {n_sta_data} stations → 0 new picks; rolling={n_roll}")
        return n_new

    def _startup_rescan(self, torch, Stream, Trace, UTCDateTime):
        """Scan the last RESCAN_WINDOW_S (≈3 min) window ONCE right after the picker
        starts — recovers events missed during restart downtime. Waits for the
        ring buffer backfill to fill (or times out) before scanning."""
        # Wait for backfill data: at least one station has enough samples,
        # or a 25 s timeout (if the backfill is slow/empty, just continue).
        deadline = time.time() + 25.0
        while time.time() < deadline:
            with self._lock:
                if not self._running:
                    return
            ready = 0
            for (net, sta), cha in list(self._station_channels.items()):
                t, _v = _LIVE_SESSION.get_window(net, sta, "", cha,
                                                 seconds=RESCAN_WINDOW_S)
                if len(t) > 200:
                    ready += 1
                    if ready >= 1:
                        break
            if ready >= 1:
                break
            time.sleep(1.0)

        with self._lock:
            if not self._running:
                return
        engine_log("phasenet", "info",
                   f"restart rescan: scanning the last {RESCAN_WINDOW_S:.0f}s "
                   f"for missed events…")
        n_new = self._run_cycle(torch, Stream, Trace, UTCDateTime,
                                window_s=RESCAN_WINDOW_S)
        self.n_cycles += 1
        self.n_picks_total += n_new
        self.last_cycle_time = time.time()
        engine_log("phasenet", "info",
                   f"restart rescan complete → +{n_new} picks recovered")

    def snapshot_picks(self) -> list[dict]:
        with self._lock:
            return list(self.rolling_picks)

    def add_bridge_pick(self, net: str, sta: str, phase: str, t: float):
        """Pick from the scautopick bridge → rolling_picks so GaMMA can associate it.
        Only processed when the picker is running AND the station is in the active streams.
        phase_amp=1.0 (not 0) so GaMMA's log10 conversion never produces -inf."""
        with self._lock:
            if not self._running:
                return
            cha = self._station_channels.get((net, sta), "")
            if not cha:
                return
            self.rolling_picks.append({
                "network"    : net, "station": sta, "location": "",
                "channel"    : cha,
                "phase_hint" : phase,
                "phase_time" : _fmt_t(t),
                "phase_score": 1.0,
                "phase_amp"  : 1.0,
            })
        # Outside the lock — proof scautopick is INTEGRATED: every accepted bridge
        # pick entering rolling (for GaMMA) is recorded in the engine log.
        engine_log("scautopick", "info", f"pick {phase} {net}.{sta} accepted → rolling")


class RealtimeAssociator:
    """Association (GaMMA, self-contained — no NLLoc grid) + real-time ML
    magnitude from the ring buffer. Runs in its own thread, separate from RealtimePicker."""

    def __init__(self, picker: RealtimePicker):
        self._picker = picker
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._base_dir = ""
        self._station_rows: list[dict] = []   # [{net,sta,lat,lon,elev}]
        self._inventory_path = ""
        self._inventory_paths: list[str] = []
        self._sds_path = ""                   # SeisComP SDS archive (Task 2) — optional
        self._work_dir: Path | None = None
        self.n_cycles = 0
        self.n_events_total = 0
        self.last_cycle_time = None
        self.error = None
        self.events: list[dict] = []   # newest at index 0
        # Per-cycle association log (Task 3) — whether events were detected or not.
        self.assoc_log: deque = deque(maxlen=120)
        # Cache for ML magnitude (coefficients + response inventory) — loaded once.
        self._ml_ref = None
        self._ml_inv = None
        # ── NLLoc refine (grid GLOBAL iasp91) — config + state ──
        self._nlloc_enabled  = NLLOC_REFINE_ENABLED
        self._nlloc_grid_dir = NLLOC_GRID_DIR
        self._nlloc_profile  = NLLOC_PROFILE
        self._nlloc_warned   = False   # log "binary/grid missing" only once
        # The GLOBAL IASP91 grid is generated at runtime for the active network → NLLoc
        # refinement is portable to any network/region (not just the bundled IA/Halmahera grid).
        self._nlloc_global   = True
        # GaMMA & region parameters overridable via the UI at start
        self._gamma_cfg_override: dict = {}
        # Association backend: "real" (default, travel-time grid, stable) | "gamma"
        # | "pyocto" (4D octree + EDT) | "glass3" (USGS NEIC grid/Bayesian stacking)
        self._assoc_backend  = ASSOC_BACKEND_DEFAULT
        self._real_warned    = False   # log "biner REAL tak ada" sekali saja
        self._glass3_warned  = False   # log "biner glass-app tak ada" sekali saja

    def _backend_label(self) -> str:
        return {"real": "REAL", "gamma": "GaMMA",
                "pyocto": "PyOcto", "glass3": "glass3"}.get(self._assoc_backend, "REAL")

    def _min_picks(self) -> int:
        """Minimum P/S phase picks required per event — shared by REAL (np0/nps0
        threshold) and GaMMA (min_picks_per_eq), adjustable from the UI."""
        return int(self._gamma_cfg_override.get("min_picks_per_eq", MIN_NSTA_EVENT))

    def _min_stations(self) -> int:
        """Minimum distinct recording stations required to accept an event —
        adjustable from the UI, independent of the pick-count threshold."""
        return int(self._gamma_cfg_override.get("min_stations", MIN_NSTA_EVENT))

    def start(self, base_dir: str, station_rows: list[dict],
              inventory_path: str | list[str],
              sds_path: str = "", gamma_cfg: dict | None = None,
              assoc_backend: str | None = None) -> bool:
        with self._lock:
            if self._running:
                return False
            self._base_dir = base_dir
            self._station_rows = station_rows
            # Accept one inventory XML or several (multi-seedlink sessions).
            if isinstance(inventory_path, (list, tuple)):
                self._inventory_paths = [p for p in inventory_path if p]
            else:
                self._inventory_paths = [inventory_path] if inventory_path else []
            self._inventory_path = self._inventory_paths[0] if self._inventory_paths else ""
            self._sds_path = sds_path or ""
            self._gamma_cfg_override = dict(gamma_cfg) if gamma_cfg else {}
            # Backend from an explicit arg, or the "backend" key in gamma_cfg (UI)
            _be = (assoc_backend
                   or (gamma_cfg or {}).get("backend")
                   or ASSOC_BACKEND_DEFAULT)
            _be = str(_be).lower()
            self._assoc_backend = _be if _be in ("gamma", "pyocto", "glass3") else "real"
            self._real_warned = False
            self._ml_ref = None        # reset the magnitude cache (inventory may change)
            self._ml_inv = None
            self._work_dir = Path(tempfile.mkdtemp(prefix="seiswork_realtime_"))
            self._running = True
            self.error = None
            self.n_cycles = 0
            self.n_events_total = 0
            # Load the persistent catalog from disk — the event list is NOT lost when
            # detection restarts / the server restarts (only the in-memory list resets).
            # Dedup first (drop duplicates from rolling-window overlap).
            n_removed = dedup_catalog(base_dir)
            if n_removed:
                engine_log("gamma", "info",
                           f"startup dedup: {n_removed} duplicate events removed from the catalog")
            self.events = load_catalog(base_dir, MAX_EVENTS_KEPT)
            self.assoc_log.clear()
        self._write_station_file()
        _be_label = self._backend_label()
        engine_log("gamma", "info",
                   f"{_be_label} associator START — {len(station_rows)} stations, "
                   f"SDS {'connected' if self._sds_path else 'none'}")
        # NLLoc refine status info (GLOBAL grid) at start — evidence for the UI/log.
        nlinfo = self.nlloc_info()
        if nlinfo["active"]:
            engine_log("gamma", "info",
                       f"NLLoc refine ACTIVE — model {nlinfo['profile']} "
                       f"(grid: {nlinfo['grid_dir']})")
        else:
            engine_log("gamma", "info",
                       f"NLLoc refine INACTIVE ({nlinfo['reason']}) — "
                       f"location uses {_be_label} initial estimate")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        with self._lock:
            self._running = False
        _be_label = self._backend_label()
        engine_log("gamma", "info", f"{_be_label} associator STOP")

    def nlloc_info(self) -> dict:
        """NonLinLoc refine feature status (for UI/log). active=True only when
        enabled, the NLLoc binary exists, AND a travel-time grid is available."""
        info = {
            "enabled"  : bool(self._nlloc_enabled),
            "profile"  : self._nlloc_profile,
            "grid_dir" : self._nlloc_grid_dir,
            "bundled"  : "config/nlloc_grids" in str(self._nlloc_grid_dir),
            "has_grid" : False,
            "has_bin"  : False,
            "active"   : False,
            "reason"   : "",
        }
        if not self._nlloc_enabled:
            info["reason"] = "disabled (NLLOC_REFINE_ENABLED=False)"
            return info
        gd = Path(self._nlloc_grid_dir)
        info["has_grid"] = gd.is_dir() and any(gd.glob(f"{self._nlloc_profile}.P.*.time.hdr"))
        info["has_bin"]  = bool(shutil.which("NLLoc"))
        if not info["has_grid"]:
            info["reason"] = f"grid not found: {self._nlloc_grid_dir}"
        elif not info["has_bin"]:
            info["reason"] = "NLLoc binary not found on PATH"
        else:
            info["active"] = True
        return info

    def status(self) -> dict:
        with self._lock:
            log = list(self.assoc_log)[-40:]   # last 40 entries for the UI
        return {
            "running"        : self._running,
            "backend"        : self._assoc_backend,   # "real" | "gamma"
            "n_cycles"       : self.n_cycles,
            "n_events_total" : self.n_events_total,
            "n_events_kept"  : len(self.events),
            "nlloc"          : self.nlloc_info(),   # NLLoc refine info for the UI
            "last_cycle_time": self.last_cycle_time,
            "error"          : self.error,
            "sds_path"       : self._sds_path,
            "assoc_log"      : log,
        }

    def _write_station_file(self):
        path = self._work_dir / "stations.txt"
        with open(path, "w") as f:
            for s in self._station_rows:
                f.write(f"{s['net']}|{s['sta']}||{s['lat']}|{s['lon']}|{s.get('elev', 0)}\n")
        self._station_file = str(path)

    def _region_cfg(self) -> dict:
        # A user-drawn monitoring area (Set Area) overrides the auto bbox: its
        # midpoint becomes the region center → REAL lat_center + NLLoc TRANS
        # origin follow the box, not the station centroid.
        ov_reg = self._gamma_cfg_override.get("region") or {}
        if all(k in ov_reg for k in ("lat_min", "lat_max", "lon_min", "lon_max")):
            lat_min, lat_max = float(ov_reg["lat_min"]), float(ov_reg["lat_max"])
            lon_min, lon_max = float(ov_reg["lon_min"]), float(ov_reg["lon_max"])
            return {
                "lat": (lat_min + lat_max) / 2, "lon": (lon_min + lon_max) / 2,
                "lat_min": lat_min, "lat_max": lat_max,
                "lon_min": lon_min, "lon_max": lon_max,
                "depth_max": float(ov_reg.get("depth_max",
                                   self._gamma_cfg_override.get("depth_max", 60.0))),
            }
        lats = [s["lat"] for s in self._station_rows]
        lons = [s["lon"] for s in self._station_rows]
        pad = 0.5
        lat_min, lat_max = min(lats) - pad, max(lats) + pad
        lon_min, lon_max = min(lons) - pad, max(lons) + pad
        return {
            "lat": (lat_min + lat_max) / 2, "lon": (lon_min + lon_max) / 2,
            "lat_min": lat_min, "lat_max": lat_max,
            "lon_min": lon_min, "lon_max": lon_max,
            "depth_max": float(self._gamma_cfg_override.get("depth_max", 60.0)),
        }

    def _nlloc_grid_tool(self, name: str) -> str:
        """Path to a NonLinLoc binary (Vel2Grid/Grid2Time) — prefers the bundled core/bin."""
        cand = Path(__file__).resolve().parents[2] / "core" / "bin" / name
        if cand.exists():
            return str(cand)
        import shutil as _sh
        return _sh.which(name) or ""

    def _ensure_global_grids(self):
        """Generate an IASP91 travel-time grid (TRANS SIMPLE centered on the active
        network) so NLLoc refinement works for ANY network/region — not just the
        IA/Halmahera network (the bundled grid). Cached by network fingerprint.
        On failure → falls back to the bundled grid. Called once at start."""
        if not self._nlloc_global or not self._nlloc_enabled:
            return
        try:
            from seiswork.modules.locator.nlloc_grids import ensure_global_grids
            cache_root = str(Path(self._base_dir) / "work" / "nlloc_global_grids")
            v2g = self._nlloc_grid_tool("Vel2Grid")
            g2t = self._nlloc_grid_tool("Grid2Time")
            # Center the TRANS origin on the monitoring-area midpoint when a
            # region override is active (Set Area), else on the station centroid.
            reg = self._region_cfg()
            center = ((reg["lat"], reg["lon"])
                      if (self._gamma_cfg_override.get("region") or {}).get("limit")
                      else None)
            gdir, prefix = ensure_global_grids(
                self._station_rows, cache_root, v2g, g2t, center=center,
                log=lambda m: engine_log("gamma", "info", f"[NLLoc-grid] {m}"))
            if gdir:
                self._nlloc_grid_dir = gdir
                self._nlloc_profile  = prefix
                engine_log("gamma", "info",
                           f"NLLoc refine using a runtime GLOBAL IASP91 grid "
                           f"({len(self._station_rows)} stations) — portable to any network")
            else:
                engine_log("gamma", "warn",
                           "[NLLoc-grid] generation failed — falling back to the bundled grid")
        except Exception as exc:   # noqa: BLE001 — never block the associator
            engine_log("gamma", "warn", f"[NLLoc-grid] error: {exc} — fallback bundled")

    def _loop(self):
        # Prepare the global IASP91 grid for the active network BEFORE the first cycle
        # (portable NLLoc refinement). On failure → fall back to the bundled grid.
        try:
            self._ensure_global_grids()
        except Exception:  # noqa: BLE001
            pass
        while True:
            with self._lock:
                if not self._running:
                    break
            t0 = time.time()
            try:
                n_ev = self._run_cycle()
                self.n_cycles += 1
                self.n_events_total += n_ev
                self.last_cycle_time = time.time()
                self.error = None
            except Exception as exc:   # noqa: BLE001
                self.error = str(exc)
            elapsed = time.time() - t0
            time.sleep(max(1.0, ASSOC_INTERVAL_S - elapsed))

    def _log(self, status: str, npicks: int, msg: str):
        """Tambah 1 entri ke log asosiasi (Task 3) DAN ke engine log terpadu
        (source=gamma). status ∈ {waiting, no_event, event, error}."""
        with self._lock:
            self.assoc_log.append({
                "t"     : time.time(),
                "status": status,
                "npicks": npicks,
                "msg"   : msg,
            })
        # Map status asosiasi → level engine log (warna): event→event,
        # error→error, lainnya→info. Bukti GaMMA jalan di engine log gabungan.
        level = "event" if status == "event" else ("error" if status == "error" else "info")
        engine_log("gamma", level, msg)

    def _refine_with_nlloc(self, ev_row, picks_gamma, pd):
        """Refine ONE GaMMA event with NonLinLoc (global iasp91 grid).

        Writes a 1-event catalog + its associated picks to an isolated dir, runs
        NLLocLocator (global travel-time grid — the module does NOT build the grid),
        then returns the refined {lat, lon, depth_km, rms}. Returns None when
        NLLoc fails / the binary/grid is missing / no solution → the caller falls back
        to the GaMMA location (the event still enters the catalog)."""
        if not self._nlloc_enabled or picks_gamma is None:
            return None
        if not Path(self._nlloc_grid_dir).exists():
            if not self._nlloc_warned:
                self._log("error", 0,
                          f"[NLLoc] global grid not found: {self._nlloc_grid_dir} "
                          f"— refine disabled, using the GaMMA location")
                self._nlloc_warned = True
            return None
        try:
            from seiswork.modules.locator.nlloc import NLLocLocator
            eid = str(ev_row.get("event_id", ""))
            # Only this event's picks (event_index == event_id) → needs ≥ min_picks
            ev_picks = picks_gamma[picks_gamma["event_index"].astype(str) == eid]
            if len(ev_picks) < self._min_picks():
                return None
            # Isolated working dir per event (avoids clashes between cycles/events)
            rdir = Path(self._work_dir) / "nlloc_refine" / f"{int(time.time()*1000)}_{eid}"
            rdir.mkdir(parents=True, exist_ok=True)
            # One-row catalog + picks_gamma.csv in the SAME dir (read by _catalog_to_obs)
            cat_path = rdir / "catalog_gamma.csv"
            pd.DataFrame([ev_row]).to_csv(cat_path, index=False)
            ev_picks.to_csv(rdir / "picks_gamma.csv", index=False)
            cfg = {
                "region": self._region_cfg(),
                "locate": {"nlloc": {
                    "exec"        : "NLLoc",
                    "grid_dir"    : self._nlloc_grid_dir,
                    "model_prefix": self._nlloc_profile,
                    "profile"     : self._nlloc_profile,
                    "save_scatter": False,
                }},
            }
            nl = NLLocLocator(cfg, self._base_dir)
            if not nl.nlloc_exec:
                if not self._nlloc_warned:
                    self._log("error", 0,
                              "[NLLoc] NLLoc binary not found — refine disabled, "
                              "using the GaMMA location")
                    self._nlloc_warned = True
                return None
            # Redirect ALL NLLoc output to an isolated dir (never touch the shared work/)
            nl.out_dir = str(rdir / "loc_out"); os.makedirs(nl.out_dir, exist_ok=True)
            nl.log_dir = str(rdir / "log");     os.makedirs(nl.log_dir, exist_ok=True)
            nl.cat_dir = str(rdir / "cat");     os.makedirs(nl.cat_dir, exist_ok=True)
            out_path = nl.run(str(cat_path))
            if not out_path or not Path(out_path).exists():
                return None
            rdf = pd.read_csv(out_path)
            if rdf.empty:
                return None
            r = rdf.iloc[0]
            rms = None
            if "rms" in rdf.columns and pd.notna(r.get("rms")):
                rms = float(r["rms"])
            return {"lat": float(r["lat"]), "lon": float(r["lon"]),
                    "depth_km": float(r["depth_km"]), "rms": rms}
        except Exception as exc:   # noqa: BLE001 — a failed refine must not drop the event
            self._log("error", 0, f"[NLLoc] refine failed: {exc}")
            return None

    # ── REAL associator (grid travel-time, deterministik) ─────────────────────
    def _real_exec_path(self) -> str:
        """Path to the REAL binary — FROM the seiswork source tree (core/bin/REAL), not the
        global PATH. Repo-relative so it's portable to other installs (same principle
        VELEST/Hypoinverse)."""
        cand = Path(__file__).resolve().parents[2] / "core" / "bin" / "REAL"
        if cand.exists():
            return str(cand)
        alt = Path(self._base_dir) / "core" / "bin" / "REAL"
        return str(alt) if alt.exists() else "REAL"

    def _real_cfg(self) -> dict:
        reg = self._region_cfg()
        ov  = self._gamma_cfg_override
        span = max(reg["lat_max"] - reg["lat_min"], reg["lon_max"] - reg["lon_min"])
        rx   = round(span / 2 + 0.5, 2)          # half-range search lat/lon (deg)
        rh   = float(ov.get("depth_max", 60.0))  # range kedalaman search (km)
        real = {
            "exec"      : self._real_exec_path(),
            "lat_center": reg["lat"],
            # -R rx/rh/tdx/tdh/tint : grid lat-lon (deg) / depth (km) / interval event.
            # COARSE grid (tdx 0.1°≈11 km, tdh 5 km) — realtime only needs FAST
            # ASSOCIATION; precise locations come from NLLoc. A fine grid (0.02°) makes
            # REAL take ~10 min/cycle (300k nodes) → unusable in realtime.
            # gcarc0 (deg): max station-to-event distance REAL will use a pick
            # from. Left unset, REAL self-widens this to the LARGEST inter-
            # station distance in the whole live session (see real.py) — with
            # 470+ stations spanning Sumatra-to-PNG that is thousands of km,
            # which is how picks from stations that far apart were getting
            # merged into one "event". Capped here to a regional/local radius.
            "search"    : {"rx": min(rx, 1.5), "rh": rh, "tdx": 0.1, "tdh": 5.0,
                           "tint": 10.0, "gcarc0": 3.0},
            # -V vp0/vs0 : without tt_db (-G) → homogeneous 1-D travel-times; enough for
            # ASSOCIATION (the final location still comes from the NLLoc iasp91 grid).
            "velocity"  : {"vp0": float(ov.get("vp0", 6.2)),
                           "vs0": float(ov.get("vs0", 3.4))},
            # -S np0/ns0/nps0/npsboth0/std0/dtps/nrt/drt : jaring IA short-period
            # DOMINAN komponen-Z (P) → JANGAN wajibkan S (ns0=0, npsboth0=0).
            "threshold" : {"np0": self._min_picks(), "ns0": 0,
                           "nps0": self._min_picks(), "npsboth0": 0,
                           "std0": float(ov.get("std0", 0.5)), "dtps": 0.1,
                           "nrt": 1.5, "drt": 0.0},
            "n_workers" : 1,
        }
        return {
            "region"   : reg,
            "associate": {"real": real},
            "data"     : {"station_file": self._station_file,
                          "inventory": self._inventory_path},
        }

    def _associate_real(self, picks_csv, npk, pd):
        """Run RealAssociator (output isolated to the session work dir), then
        return (catalog_df, picks_compat) — picks_compat is a superset frame.
        (None, None) = technical failure; (empty_df, None) = no event."""
        from seiswork.modules.associator.real import RealAssociator
        assoc = RealAssociator(self._real_cfg(), self._base_dir)
        # ISOLATION: never write into the shared work/catalog|real
        assoc.cat_dir  = str(self._work_dir / "catalog_real")
        assoc.real_dir = str(self._work_dir / "real_run")
        assoc.log_dir  = str(self._work_dir / "real_log")
        for d in (assoc.cat_dir, assoc.real_dir, assoc.log_dir):
            os.makedirs(d, exist_ok=True)
        if not assoc.real_exec:
            if not self._real_warned:
                self._log("error", npk,
                          "REAL: binary not found at core/bin/REAL — association skipped")
                self._real_warned = True
            return None, None
        try:
            out_cat = assoc.run(str(picks_csv))
        except SystemExit:
            self.error = "RealAssociator: invalid station file / binary"
            self._log("error", npk, "REAL: invalid station file / binary")
            return None, None
        except Exception as exc:   # noqa: BLE001
            self._log("error", npk, f"REAL failed: {exc}")
            return None, None
        if not out_cat or not Path(out_cat).exists():
            return pd.DataFrame(), None
        catalog_df = pd.read_csv(out_cat)
        if catalog_df.empty:
            return pd.DataFrame(), None
        # Build picks_compat (superset) from picks_real.csv
        picks_compat = None
        try:
            pr = pd.read_csv(Path(assoc.cat_dir) / "picks_real.csv")
            pr["event_index"] = pr["event_id"]
            pr["_sta_key"]    = pr["network"].astype(str) + "." + pr["station"].astype(str)
            pr["id"]          = pr["_sta_key"]
            pr["type"]        = pr["phase"]
            pr["timestamp"]   = pr["pick_time"]
            picks_compat = pr
        except Exception:
            picks_compat = None
        return catalog_df, picks_compat

    def _associate_gamma(self, picks_csv, npk, pd):
        """Run GammaAssociator (the legacy path). Same return contract as
        _associate_real: (catalog_df, picks_gamma) | (None,None) | (empty,None)."""
        from seiswork.modules.associator.gamma import GammaAssociator
        _gcfg = {"min_picks_per_eq": self._min_picks()}   # default ketat (sparse network)
        for _k in ("min_picks_per_eq", "max_sigma", "use_amplitude", "method",
                   "oversample_factor", "dbscan_eps", "dbscan_min_samples"):
            if _k in self._gamma_cfg_override:
                _gcfg[_k] = self._gamma_cfg_override[_k]
        cfg = {
            "region"   : self._region_cfg(),
            "associate": {"gamma": _gcfg},
            "data"     : {"station_file": self._station_file,
                          "inventory": self._inventory_path},
        }
        assoc = GammaAssociator(cfg, self._base_dir)
        assoc.catalog_dir = str(self._work_dir / "catalog")  # ISOLATION
        try:
            out_cat = assoc.run(str(picks_csv))
        except SystemExit:
            self.error = "GammaAssociator: invalid station file"
            self._log("error", npk, "GaMMA: invalid station file")
            return None, None
        if not out_cat or not Path(out_cat).exists():
            return pd.DataFrame(), None
        catalog_df = pd.read_csv(out_cat)
        if catalog_df.empty:
            return pd.DataFrame(), None
        picks_gamma = None
        try:
            picks_gamma = pd.read_csv(Path(assoc.catalog_dir) / "picks_gamma.csv")
            picks_gamma["_sta_key"] = (
                picks_gamma["id"].astype(str)
                .str.split(".").str[:2].str.join(".")
            )
        except Exception:
            picks_gamma = None
        return catalog_df, picks_gamma

    # ── PyOcto associator (4D octree + EDT, in-process — no external binary) ──
    def _pyocto_cfg(self) -> dict:
        reg = self._region_cfg()
        ov  = self._gamma_cfg_override
        return {
            "region"   : reg,
            "associate": {"pyocto": {
                "n_picks"        : self._min_picks(),
                "n_p_picks"      : max(1, self._min_picks() - 2),
                "n_s_picks"      : 0,
                "n_p_and_s_picks": 0,
                # COARSE node size — realtime only needs FAST association;
                # precise locations come from NLLoc (same rationale as REAL's
                # coarse -R grid in _real_cfg()).
                "min_node_size"       : 15.0,
                "pick_match_tolerance": 1.5,
                "min_interevent_time" : 3.0,
                "time_before"         : 300.0,
                "velocity": {"vp": float(ov.get("vp0", 6.2)),
                             "vs": float(ov.get("vs0", 3.4))},
            }},
            "data": {"station_file": self._station_file,
                     "inventory": self._inventory_path},
        }

    def _associate_pyocto(self, picks_csv, npk, pd):
        """Run PyOctoAssociator (output isolated to the session work dir), then
        return (catalog_df, picks_compat) — same contract as _associate_real."""
        from seiswork.modules.associator.pyocto import PyOctoAssociator
        assoc = PyOctoAssociator(self._pyocto_cfg(), self._base_dir)
        assoc.catalog_dir = str(self._work_dir / "catalog_pyocto")  # ISOLATION
        try:
            out_cat = assoc.run(str(picks_csv))
        except SystemExit:
            self.error = "PyOctoAssociator: invalid station file"
            self._log("error", npk, "PyOcto: invalid station file")
            return None, None
        except Exception as exc:   # noqa: BLE001
            self._log("error", npk, f"PyOcto failed: {exc}")
            return None, None
        if not out_cat or not Path(out_cat).exists():
            return pd.DataFrame(), None
        catalog_df = pd.read_csv(out_cat)
        if catalog_df.empty:
            return pd.DataFrame(), None
        picks_compat = None
        try:
            pk = pd.read_csv(Path(assoc.catalog_dir) / "picks_pyocto.csv")
            pk["event_index"] = pk["event_id"]
            pk["_sta_key"]    = pk["network"].astype(str) + "." + pk["station"].astype(str)
            pk["id"]          = pk["_sta_key"]
            pk["type"]        = pk["phase"]
            pk["timestamp"]   = pk["pick_time"]
            picks_compat = pk
        except Exception:
            picks_compat = None
        return catalog_df, picks_compat

    # ── glass3 associator (USGS NEIC grid/Bayesian stacking, glass-app subprocess) ──
    def _glass3_exec_path(self) -> str:
        """Path to the glass-app binary — FROM the seiswork source tree
        (core/bin/glass-app), same principle as _real_exec_path()."""
        cand = Path(__file__).resolve().parents[2] / "core" / "bin" / "glass-app"
        if cand.exists():
            return str(cand)
        alt = Path(self._base_dir) / "core" / "bin" / "glass-app"
        return str(alt) if alt.exists() else "glass-app"

    def _glass3_cfg(self) -> dict:
        reg = self._region_cfg()
        return {
            "region"   : reg,
            "associate": {"glass3": {
                "exec"                            : self._glass3_exec_path(),
                "nucleation_data_count_threshold"  : self._min_picks(),
                "nucleation_stack_threshold"       : 2.5,
                "association_sd_cutoff"            : 6.0,
                # COARSE grid + short shutdown_wait — realtime cycles run on a
                # small rolling snapshot, so glass-app needs to finish and
                # self-exit well within ASSOC_INTERVAL_S (same rationale as
                # REAL's coarse -R grid in _real_cfg()).
                "node_resolution_km"       : 20.0,
                "num_stations_per_node"    : 10,
                "n_threads"                : 2,
                "shutdown_wait"            : 15,
            }},
            "data": {"station_file": self._station_file,
                     "inventory": self._inventory_path},
        }

    def _associate_glass3(self, picks_csv, npk, pd):
        """Run Glass3Associator (output isolated to the session work dir), then
        return (catalog_df, picks_compat) — same contract as _associate_real."""
        from seiswork.modules.associator.glass3 import Glass3Associator
        assoc = Glass3Associator(self._glass3_cfg(), self._base_dir)
        # ISOLATION: never write into the shared work/catalog|glass3
        assoc.cat_dir   = str(self._work_dir / "catalog_glass3")
        assoc.glass_dir = str(self._work_dir / "glass3_run")
        assoc.log_dir   = str(self._work_dir / "glass3_log")
        for d in (assoc.cat_dir, assoc.glass_dir, assoc.log_dir):
            os.makedirs(d, exist_ok=True)
        if not assoc.glass_exec:
            if not self._glass3_warned:
                self._log("error", npk,
                          "glass3: glass-app binary not found at core/bin/glass-app "
                          "— association skipped")
                self._glass3_warned = True
            return None, None
        try:
            out_cat = assoc.run(str(picks_csv))
        except SystemExit:
            self.error = "Glass3Associator: invalid station file / binary"
            self._log("error", npk, "glass3: invalid station file / binary")
            return None, None
        except Exception as exc:   # noqa: BLE001
            self._log("error", npk, f"glass3 failed: {exc}")
            return None, None
        if not out_cat or not Path(out_cat).exists():
            return pd.DataFrame(), None
        catalog_df = pd.read_csv(out_cat)
        if catalog_df.empty:
            return pd.DataFrame(), None
        picks_compat = None
        try:
            pk = pd.read_csv(Path(assoc.cat_dir) / "picks_glass3.csv")
            pk["event_index"] = pk["event_id"]
            pk["_sta_key"]    = pk["network"].astype(str) + "." + pk["station"].astype(str)
            pk["id"]          = pk["_sta_key"]
            pk["type"]        = pk["phase"]
            pk["timestamp"]   = pk["pick_time"]
            picks_compat = pk
        except Exception:
            picks_compat = None
        return catalog_df, picks_compat

    def _run_cycle(self) -> int:
        import pandas as pd
        picks = self._picker.snapshot_picks()
        npk = len(picks)
        if npk < self._min_picks():
            self._log("waiting", npk,
                      f"{npk} pick terkumpul (perlu ≥{self._min_picks()}) — menunggu")
            return 0

        picks_csv = self._work_dir / "picks.csv"
        pd.DataFrame(picks).to_csv(picks_csv, index=False)

        # ── Association: REAL (default, stable) | GaMMA | PyOcto | glass3 ───────
        # All backends return (catalog_df, picks_compat). picks_compat
        # is a SUPERSET frame: event-loop columns (event_index/_sta_key/type/
        # timestamp) AND NLLoc-obs columns (network/station/phase/pick_time) — so
        # the event loop + _refine_with_nlloc below need not know which backend ran.
        if self._assoc_backend == "real":
            catalog_df, picks_gamma = self._associate_real(picks_csv, npk, pd)
        elif self._assoc_backend == "pyocto":
            catalog_df, picks_gamma = self._associate_pyocto(picks_csv, npk, pd)
        elif self._assoc_backend == "glass3":
            catalog_df, picks_gamma = self._associate_glass3(picks_csv, npk, pd)
        else:
            catalog_df, picks_gamma = self._associate_gamma(picks_csv, npk, pd)
        if catalog_df is None:
            return 0   # technical failure (invalid binary/stations) — already logged
        if catalog_df.empty:
            self._log("no_event", npk, f"{npk} picks → no event associated")
            return 0

        n_new = 0
        for _, ev in catalog_df.iterrows():
            if int(ev.get("nsta", 0) or 0) < self._min_stations():
                continue
            def _f(key):  # safe float parse (NaN/empty → None)
                try:
                    val = float(ev.get(key, float("nan")))
                    return None if val != val else val   # NaN check
                except (TypeError, ValueError):
                    return None

            # This event's stations + P picks (from associated picks in picks_gamma.csv)
            ev_sta_list: list[str] = []
            ev_p_picks:  list[dict] = []   # [{network, station, pick_time}] for FocoNet
            if picks_gamma is not None:
                try:
                    ev_idx_str = str(ev.get("event_id", ""))
                    mask = picks_gamma["event_index"].astype(str) == ev_idx_str
                    ev_rows = picks_gamma.loc[mask]
                    ev_sta_list = ev_rows["_sta_key"].unique().tolist()
                    # Store P-pick timing — used by FocoNet without needing the live pick log
                    p_mask = ev_rows["type"].astype(str).str.upper().str.startswith("P")
                    for _, pr in ev_rows.loc[p_mask].iterrows():
                        key = str(pr.get("_sta_key", ""))
                        parts = key.split(".")
                        if len(parts) < 2:
                            continue
                        ev_p_picks.append({
                            "network" : parts[0],
                            "station" : parts[1],
                            "pick_time": str(pr.get("timestamp", "")),
                        })
                except Exception:
                    pass

            _lat, _lon = float(ev["lat"]), float(ev["lon"])
            event = {
                "event_id"    : f"rt_{int(time.time())}_{n_new}",
                "datetime"    : str(ev["datetime"]),
                "lat"         : _lat, "lon": _lon,
                "depth_km"    : float(ev["depth_km"]), "nsta": int(ev["nsta"]),
                "rms"         : _f("rms"), "gap": _f("gap"),
                "mag"         : None, "focal": None,
                # The associator used (REAL/GaMMA/PyOcto/glass3) — KEPT for GUI info
                # even though the loc method later becomes "NLLoc" after refinement.
                "assoc"       : self._backend_label(),
                # Location source label; replaced with "NLLoc" once refined
                "loc_method"  : self._backend_label(),
                "detected_at" : datetime.utcnow().isoformat(timespec="seconds"),
                "nearest_city": nearest_city_info(_lat, _lon),
                "stations"    : ev_sta_list,   # recording NET.STA
                "networks"    : _event_networks(ev_sta_list),  # source/network discriminator
                "p_picks"     : ev_p_picks,    # P-pick timing for FocoNet
            }
            # ── Spatio-temporal duplicate check (more robust than exact datetime) ──
            with self._lock:
                is_dup = is_duplicate_event(event, self.events)
            if is_dup:
                self._log("no_event", npk,
                          f"duplikat diabaikan: {event['datetime']} "
                          f"lat={event['lat']:.3f} lon={event['lon']:.3f}")
                continue
            # ── Refine the hypocenter with NonLinLoc (GLOBAL iasp91 grid) ──
            # The associator (REAL/GaMMA/PyOcto/glass3) gives association + a rough
            # location; the final location comes from NLLoc. On failure → keep the
            # associator location (loc_method unchanged).
            _be_lbl = self._backend_label()
            refined = self._refine_with_nlloc(ev, picks_gamma, pd)
            if refined is not None:
                event["lat"]        = refined["lat"]
                event["lon"]        = refined["lon"]
                event["depth_km"]   = refined["depth_km"]
                if refined["rms"] is not None:
                    event["rms"]    = refined["rms"]
                event["loc_method"] = "NLLoc"
                event["nearest_city"] = nearest_city_info(refined["lat"], refined["lon"])
                self._log("event", npk,
                          f"[NLLoc] refine OK → {refined['lat']:.3f},{refined['lon']:.3f} "
                          f"z={refined['depth_km']:.0f}km ({_be_lbl}: {_lat:.3f},{_lon:.3f})")
            try:
                event["mag"] = self._compute_magnitude(event)
            except Exception as exc:   # noqa: BLE001 — a failed magnitude must not drop the event
                event["mag_error"] = str(exc)
            with self._lock:
                self.events.insert(0, event)
                self.events = self.events[:MAX_EVENTS_KEPT]
            append_catalog(self._base_dir, event)   # persist to the disk catalog
            magstr = f"M{event['mag']:.1f}" if event["mag"] is not None else "M—"
            self._log("event", npk,
                      f"EVENT {magstr} @ {event['lat']:.3f},{event['lon']:.3f} "
                      f"z={event['depth_km']:.0f}km · {event['nsta']} sta")
            n_new += 1
        if n_new == 0:
            self._log("no_event", npk,
                      f"{npk} pick → kandidat ditolak (nsta<{self._min_stations()}/duplikat)")
        return n_new

    # ── Real-time ML magnitude from the ring buffer (NOT MLMagnitude.run() — that
    # fetches from an on-disk SDS absent in live-only sessions) — the formula & PAZ
    # are still reused 1:1 from modules/magnitude/ml.py; only the source differs. ──
    def _read_sds_trace(self, net: str, sta: str, cha: str,
                        t0, pre: float = 30.0, post: float = 90.0):
        """Read 1 trace from the SeisComP SDS archive (Task 2) via the obspy SDS Client.
        Return (Trace, sr) or (None, None). Used as a waveform source more
        reliable than the live ring buffer (which only holds 90s and may not yet
        reach the event time due to seedlink latency)."""
        if not self._sds_path or not Path(self._sds_path).exists():
            return None, None
        try:
            from obspy.clients.filesystem.sds import Client as _SDSClient
            cli = _SDSClient(self._sds_path)
            # loc "" → SDS Client cocokkan wildcard loc
            st = cli.get_waveforms(net, sta, "*", cha, t0 - pre, t0 + post)
            if not st:
                return None, None
            st.merge(method=1, fill_value="interpolate")
            tr = st[0]
            return tr, float(tr.stats.sampling_rate)
        except Exception:
            return None, None

    def _compute_magnitude(self, event: dict) -> float | None:
        from obspy import Stream, Trace, UTCDateTime
        from seiswork.modules.magnitude.ml import (MLMagnitude, VERTICAL_AMP_FACTOR,
                                                    _signal_window_end,
                                                    scmag_network_magnitude)

        # Load the a/b/c coefficients + wa_paz + inventory (for remove_response) ONCE.
        if self._ml_ref is None:
            self._ml_ref = MLMagnitude(
                {"data": {"waveform_dir": "work/_realtime_dummy"}}, self._base_dir)
            try:
                # Merge every session inventory (multi-seedlink) so response
                # removal works for stations coming from any source.
                inv_all = None
                for _p in (getattr(self, "_inventory_paths", None)
                           or [self._inventory_path]):
                    cur = self._ml_ref._load_inventory(_p)
                    if cur is None:
                        continue
                    try:
                        inv_all = cur if inv_all is None else inv_all + cur
                    except Exception:
                        inv_all = inv_all or cur
                self._ml_inv = inv_all
            except Exception:
                self._ml_inv = None
        mref = self._ml_ref
        inv  = self._ml_inv

        t0  = UTCDateTime(event["datetime"])
        now = time.time()
        # The window MUST cover the event time: events are often detected minutes
        # AFTER origin (GaMMA rolling window), so request a ring-buffer window as
        # wide as the event age + coda (capped at 30 min). The server ring buffer keeps 1 day.
        # Also widened per-station below (win_end) so far stations' S-wave/coda —
        # which arrives later — isn't cut off by a fixed post-origin window.
        win = max(180.0, min((now - t0.timestamp) + 90.0, 1800.0))

        ml_values = []
        n_dist_ok = 0   # stations within range
        n_data    = 0   # stations whose waveform covers the event
        n_resp_fail = 0 # stations skipped because the response could not be removed
        for s in self._station_rows:
            dist = mref._dist_km(event["lat"], event["lon"], s["lat"], s["lon"], event["depth_km"])
            if dist > mref.dist_max or dist < 1.0:
                continue
            n_dist_ok += 1
            win_end = _signal_window_end(dist)
            cha = s.get("default_channel", "")
            # Source 1: live ring buffer (holds up to 1 day). Source 2: SeisComP SDS.
            tr = None
            t, v = _LIVE_SESSION.get_window(s["net"], s["sta"], "", cha,
                                            seconds=max(win, win_end + 20.0))
            sr = _LIVE_SESSION.get_sampling_rate(s["net"], s["sta"], "", cha)
            if len(t) >= 10 and sr and t[0] <= t0.timestamp <= t[-1]:
                tr = Trace(data=v.astype(np.float64))
                tr.stats.network = s["net"]; tr.stats.station = s["sta"]
                tr.stats.location = s.get("default_loc", ""); tr.stats.channel = cha
                tr.stats.sampling_rate = float(sr)
                tr.stats.starttime = UTCDateTime(float(t[0]))
            else:
                tr_sds, sr_sds = self._read_sds_trace(s["net"], s["sta"], cha, t0,
                                                      post=win_end + 20.0)
                if tr_sds is not None:
                    tr = tr_sds; sr = sr_sds
            if tr is None or not sr:
                continue
            n_data += 1
            try:
                st = Stream([tr])
                st.detrend("demean")
                st.taper(0.05)
                # The instrument response MUST be removed first (→ DISP) before simulating
                # Wood-Anderson. WITHOUT remove_response, amplitudes remain in
                # COUNTS (≈ counts × WA gain ~2080) → severe ML over-estimation
                # (e.g. M12). If the response CANNOT be removed (missing inventory,
                # or loc-code/epoch mismatch with metadata), SKIP this station
                # — NEVER compute ML from raw counts (that was the M12 bug).
                if inv is None:
                    n_resp_fail += 1
                    continue
                try:
                    st.remove_response(inventory=inv, output="DISP",
                                       pre_filt=[0.5, 1.0, 20.0, 25.0])
                except Exception:
                    n_resp_fail += 1
                    continue
                st.simulate(paz_remove=None, paz_simulate=mref.wa_paz)
                tr = st[0]
                srr = tr.stats.sampling_rate
                i0 = max(0, int((t0 - tr.stats.starttime - 5) * srr))
                i1 = int((t0 - tr.stats.starttime + win_end) * srr)
                data = tr.data[i0:i1]
                if len(data) < 10:
                    continue
                # SeisComP MLv convention: a vertical-only amplitude is doubled
                # before entering the (horizontal-calibrated) ML formula — see
                # amplitudes/MLv.cpp. All SeisWork stations are BHZ-only.
                amp_mm = (np.max(data) - np.min(data)) / 2.0 * 1000.0 * VERTICAL_AMP_FACTOR
                if amp_mm <= 0:
                    continue
                ml = np.log10(amp_mm) + mref.a * np.log10(dist) + mref.c * dist + mref.b
                ml_values.append(ml)
            except Exception:
                continue
        # Network magnitude: scmag's default averaging rule — trimmed mean(25%)
        # for more than 3 station magnitudes, plain mean otherwise (was plain
        # median before, which doesn't match SeisComP's scmag behavior).
        val, stdev, method_id = scmag_network_magnitude(ml_values)
        # Diagnostics to the engine log — makes it visible WHY magnitude failed.
        engine_log("gamma", "info" if val is not None else "warn",
                   f"magnitude: {len(ml_values)}/{n_dist_ok} stations used "
                   f"(data {n_data}, response failed {n_resp_fail}, "
                   f"inv {'yes' if inv is not None else 'no'}) → "
                   + (f"ML {val:.2f} ± {stdev:.2f} ({method_id})" if val is not None
                      else "FAILED — insufficient data/response"))
        return val


_REALTIME_PICKER: RealtimePicker | None = None
_REALTIME_ASSOC: RealtimeAssociator | None = None


def get_realtime_picker() -> RealtimePicker:
    global _REALTIME_PICKER
    if _REALTIME_PICKER is None:
        _REALTIME_PICKER = RealtimePicker()
    return _REALTIME_PICKER


def get_realtime_associator() -> RealtimeAssociator:
    global _REALTIME_ASSOC
    if _REALTIME_ASSOC is None:
        _REALTIME_ASSOC = RealtimeAssociator(get_realtime_picker())
    return _REALTIME_ASSOC


# ── Backfill: re-pick + re-associate from SDS for missed periods ─────────────
# Global state for a single backfill job (only 1 runs at a time).
_BACKFILL_STATE: dict = {
    "running": False, "progress": 0, "total": 0,
    "n_picks": 0, "n_events": 0, "error": None,
    "log": [], "started_at": None, "done_at": None,
    "start_time": None, "end_time": None,
}
_BACKFILL_LOCK = threading.Lock()
_BACKFILL_MODEL_LOCK = threading.Lock()   # serialize GPU/CPU inference


def get_backfill_state() -> dict:
    with _BACKFILL_LOCK:
        return dict(_BACKFILL_STATE)


def _backfill_log(msg: str, level: str = "info"):
    engine_log("system", level, f"[backfill] {msg}")
    with _BACKFILL_LOCK:
        _BACKFILL_STATE["log"].append({"t": time.time(), "level": level, "msg": msg})
        _BACKFILL_STATE["log"] = _BACKFILL_STATE["log"][-300:]


def run_backfill(start_t: float, end_t: float, sds_path: str,
                 station_rows: list[dict], inventory_path: str,
                 base_dir: str):
    """Re-pick with PhaseNet + re-associate with GaMMA from SDS for the [start_t, end_t] period.

    Runs in a background thread. Status via get_backfill_state().
    New events found (not catalog duplicates) are appended to the disk catalog.
    """
    with _BACKFILL_LOCK:
        if _BACKFILL_STATE["running"]:
            return   # one is already running
        _BACKFILL_STATE.update({
            "running": True, "progress": 0, "total": len(station_rows),
            "n_picks": 0, "n_events": 0, "error": None,
            "log": [], "started_at": time.time(), "done_at": None,
            "start_time": _fmt_t(start_t), "end_time": _fmt_t(end_t),
        })

    def _thread():
        import tempfile
        import pandas as pd
        import numpy as np
        from obspy import Stream, UTCDateTime
        from obspy.clients.filesystem.sds import Client as SDSClient

        try:
            t0_utc = UTCDateTime(start_t)
            t1_utc = UTCDateTime(end_t)
            _backfill_log(
                f"starting: {t0_utc.strftime('%Y-%m-%dT%H:%M')} → "
                f"{t1_utc.strftime('%Y-%m-%dT%H:%M')} "
                f"| SDS: {sds_path} | {len(station_rows)} stations"
            )

            # Make sure the PhaseNet model is available
            picker = get_realtime_picker()
            try:
                picker._ensure_model()
            except Exception as exc:
                with _BACKFILL_LOCK:
                    _BACKFILL_STATE.update({"running": False, "error": str(exc),
                                            "done_at": time.time()})
                _backfill_log(f"failed to load the PhaseNet model: {exc}", "error")
                return
            if picker.model is None or picker._helper is None:
                with _BACKFILL_LOCK:
                    _BACKFILL_STATE.update({"running": False,
                                            "error": "PhaseNet model not ready",
                                            "done_at": time.time()})
                return

            import torch
            sds_client = SDSClient(sds_path)
            all_picks: list[dict] = []

            for i, s in enumerate(station_rows):
                with _BACKFILL_LOCK:
                    if not _BACKFILL_STATE["running"]:
                        break   # cancelled
                    _BACKFILL_STATE["progress"] = i + 1

                net, sta = s["net"], s["sta"]
                cha = s.get("default_channel") or DEFAULT_CHANNEL
                try:
                    st = sds_client.get_waveforms(net, sta, "*", cha, t0_utc, t1_utc)
                    if not st:
                        continue
                    st.merge(method=1, fill_value="interpolate")
                    tr = st[0]
                    if len(tr.data) < 100:
                        continue
                except Exception as exc:
                    _backfill_log(f"  {net}.{sta}: failed to read SDS — {exc}", "warn")
                    continue

                key = f"{net}.{sta}"
                raw_stream = Stream([tr.copy()])
                try:
                    with _BACKFILL_MODEL_LOCK:
                        filtered = picker._helper._filter(Stream([tr]))
                        filtered = picker._denoise_stream(filtered)
                        with torch.no_grad():
                            ann = picker.model.annotate(filtered, batch_size=16)
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                except Exception as exc:
                    _backfill_log(f"  {net}.{sta}: annotate failed — {exc}", "warn")
                    continue

                df = picker._helper._extract_picks(key, ann, raw_stream)
                if df.empty:
                    continue

                for _, row in df.iterrows():
                    all_picks.append({
                        "network"    : net, "station": sta, "location": "",
                        "channel"    : str(row.get("channel", cha)),
                        "phase_hint" : str(row["phase_hint"]),
                        "phase_time" : str(row["phase_time"]),
                        "phase_score": float(row["phase_score"]),
                        "phase_amp"  : row["phase_amp"],
                    })

                with _BACKFILL_LOCK:
                    _BACKFILL_STATE["n_picks"] = len(all_picks)
                if len(df):
                    _backfill_log(f"  {net}.{sta}: {len(df)} picks")

            n_picks = len(all_picks)
            _backfill_log(f"picking complete — {n_picks} total picks from {i+1} stations")

            if n_picks < MIN_NSTA_EVENT:
                _backfill_log("too few picks for association — done")
                with _BACKFILL_LOCK:
                    _BACKFILL_STATE.update({"running": False, "done_at": time.time()})
                return

            # Write picks + the station file to an isolated tmpdir
            tmpdir = Path(tempfile.mkdtemp(prefix="seiswork_backfill_"))
            picks_csv = tmpdir / "picks.csv"
            pd.DataFrame(all_picks).to_csv(picks_csv, index=False)

            sta_file = tmpdir / "stations.txt"
            with open(sta_file, "w") as f:
                for s in station_rows:
                    f.write(f"{s['net']}|{s['sta']}||{s['lat']}|{s['lon']}|{s.get('elev',0)}\n")

            lats = [s["lat"] for s in station_rows]
            lons = [s["lon"] for s in station_rows]
            pad  = 0.5
            # Take gamma_cfg_override from the active associator (if any)
            _assoc_inst = get_realtime_associator()
            _gcfg_bf    = dict(getattr(_assoc_inst, "_gamma_cfg_override", {}))
            _depth_max_bf = float(_gcfg_bf.pop("depth_max", 60.0))
            _gcfg_bf.setdefault("min_picks_per_eq", MIN_NSTA_EVENT)

            region_cfg = {
                "lat"      : (min(lats) + max(lats)) / 2,
                "lon"      : (min(lons) + max(lons)) / 2,
                "lat_min"  : min(lats) - pad, "lat_max": max(lats) + pad,
                "lon_min"  : min(lons) - pad, "lon_max": max(lons) + pad,
                "depth_max": _depth_max_bf,
            }

            from seiswork.modules.associator.gamma import GammaAssociator
            cfg = {
                "region"   : region_cfg,
                "associate": {"gamma": _gcfg_bf},
                "data"     : {"station_file": str(sta_file),
                              "inventory"   : inventory_path},
            }
            ga = GammaAssociator(cfg, base_dir)
            ga.catalog_dir = str(tmpdir / "catalog")
            os.makedirs(ga.catalog_dir, exist_ok=True)

            _backfill_log("running GaMMA association…")
            try:
                out_cat = ga.run(str(picks_csv))
            except SystemExit:
                _backfill_log("GaMMA: invalid station file", "error")
                shutil.rmtree(tmpdir, ignore_errors=True)
                with _BACKFILL_LOCK:
                    _BACKFILL_STATE.update({"running": False, "done_at": time.time()})
                return

            if not out_cat or not Path(out_cat).exists():
                _backfill_log("GaMMA: no event associated")
                shutil.rmtree(tmpdir, ignore_errors=True)
                with _BACKFILL_LOCK:
                    _BACKFILL_STATE.update({"running": False, "done_at": time.time()})
                return

            catalog_df = pd.read_csv(out_cat)
            # Read picks_associated.csv to extract stations + p_picks
            picks_assoc_path = Path(ga.catalog_dir) / "picks_associated.csv"
            try:
                picks_assoc = pd.read_csv(picks_assoc_path)
                picks_assoc["_sta_key"] = (
                    picks_assoc["id"].astype(str)
                    .str.split(".").str[:2].str.join(".")
                )
            except Exception:
                picks_assoc = None

            existing   = load_catalog(base_dir)
            n_new = 0
            for _, ev in catalog_df.iterrows():
                if int(ev.get("nsta", 0) or 0) < MIN_NSTA_EVENT:
                    continue
                _lat, _lon = float(ev["lat"]), float(ev["lon"])

                # Extract stations + p_picks from the associated picks
                ev_sta_list: list[str] = []
                ev_p_picks:  list[dict] = []
                if picks_assoc is not None:
                    try:
                        ev_idx_str = str(ev.get("event_id", ""))
                        mask = picks_assoc["event_index"].astype(str) == ev_idx_str
                        ev_rows = picks_assoc.loc[mask]
                        ev_sta_list = ev_rows["_sta_key"].unique().tolist()
                        p_mask = ev_rows["type"].astype(str).str.upper().str.startswith("P")
                        for _, pr in ev_rows.loc[p_mask].iterrows():
                            key = str(pr.get("_sta_key", ""))
                            parts = key.split(".")
                            if len(parts) < 2:
                                continue
                            ev_p_picks.append({
                                "network" : parts[0],
                                "station" : parts[1],
                                "pick_time": str(pr.get("timestamp", "")),
                            })
                    except Exception:
                        pass

                event = {
                    "event_id"   : f"bf_{int(time.time()*1000)}_{n_new}",
                    "datetime"   : str(ev["datetime"]),
                    "lat"        : _lat, "lon": _lon,
                    "depth_km"   : float(ev["depth_km"]),
                    "nsta"       : int(ev["nsta"]),
                    "rms"        : float(ev.get("rms") or float("nan")),
                    "gap"        : None, "mag": None, "focal": None,
                    "loc_method" : "GaMMA-backfill",
                    "detected_at": datetime.utcnow().isoformat(timespec="seconds"),
                    "nearest_city": nearest_city_info(_lat, _lon),
                    "stations"   : ev_sta_list,
                    "networks"   : _event_networks(ev_sta_list),
                    "p_picks"    : ev_p_picks,
                    "backfill"   : True,
                }
                if is_duplicate_event(event, existing, time_s=60.0, dist_km=30.0):
                    _backfill_log(
                        f"  duplikat diabaikan: {event['datetime']} "
                        f"lat={_lat:.3f} lon={_lon:.3f}"
                    )
                    continue
                append_catalog(base_dir, event)
                existing.insert(0, event)
                n_new += 1
                city = event["nearest_city"].get("city", "?")
                dist = event["nearest_city"].get("dist_km", "?")
                _backfill_log(
                    f"  New EVENT: {event['datetime'][:19]} "
                    f"lat={_lat:.3f} lon={_lon:.3f} "
                    f"nsta={int(ev['nsta'])} | {city} ~{dist}km"
                )

            shutil.rmtree(tmpdir, ignore_errors=True)
            _backfill_log(
                f"done — {n_new} new events added to the catalog "
                f"(total picks: {n_picks})"
            )
            with _BACKFILL_LOCK:
                _BACKFILL_STATE.update({
                    "running" : False,
                    "n_events": n_new,
                    "done_at" : time.time(),
                })

        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            _backfill_log(f"ERROR: {exc}\n{tb}", "error")
            with _BACKFILL_LOCK:
                _BACKFILL_STATE.update({
                    "running": False, "error": str(exc), "done_at": time.time()
                })

    t = threading.Thread(target=_thread, daemon=True, name="backfill")
    t.start()

"""
SeisWork — HypoDD relocation for the online catalog (events.jsonl + p_picks).

The online catalog differs from the offline one:
- JSONL format (not CSV)
- Picks are stored in the event dict's 'p_picks' field (P-only, UTC absolute)
- Station coords come from an inventory XML (not a text station file)
- Runs as a background thread; status is saved to a status file

Flow:
  1. Load events.jsonl → filter for ones with p_picks
  2. Load station coords from the inventory XML
  3. Write event.dat + phase.dat + station.dat into work_dir
  4. Run ph2dt → hypoDD (global IASP91 model)
  5. Parse hypoDD.reloc → save hypodd_reloc.json into online_catalog/
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Status of running/finished relocations ──────────────────────────────────
# Keyed per cfg_id so a HypoDD run for one config does not block another
# running concurrently (each session/cfg_id is isolated).
_NO_CFG = "_default"   # fallback key for callers without a cfg_id

_reloc_lock    = threading.Lock()
_reloc_status: dict[str, dict] = {}   # {cfg_id: {state, started, finished, n_input, n_reloc, error}}


def get_reloc_status(cfg_id: str | None = None) -> dict:
    key = cfg_id or _NO_CFG
    with _reloc_lock:
        return dict(_reloc_status.get(key, {}))


def _set_status(cfg_id: str | None = None, **kw):
    key = cfg_id or _NO_CFG
    with _reloc_lock:
        _reloc_status.setdefault(key, {}).update(kw)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_exec(name: str, base_dir: str) -> str:
    for cand in (
        Path(base_dir) / "core" / "bin" / name,
        Path(__file__).resolve().parents[2] / "core" / "bin" / name,
    ):
        if cand.exists():
            return str(cand)
    return shutil.which(name) or ""


def _load_station_coords(inventory_path: str) -> dict[str, tuple[float, float, float]]:
    """Read every available inventory → {NET.STA: (lat, lon, elev_km)}.

    Besides `inventory_path`, also reads every XML in work/online_inventory/
    so stations from online sessions (Palu, etc.) are included.
    """
    from obspy import read_inventory

    paths_to_try: list[str] = []
    if inventory_path and os.path.exists(inventory_path):
        paths_to_try.append(inventory_path)

    # Add every XML in work/online_inventory/ one level above config/
    base_guess = os.path.dirname(os.path.dirname(inventory_path)) if inventory_path else ""
    for extra_dir in [
        os.path.join(base_guess, "work", "online_inventory"),
        os.path.join(os.path.dirname(inventory_path or ""), "..", "work", "online_inventory"),
    ]:
        extra_dir = os.path.normpath(extra_dir)
        if os.path.isdir(extra_dir):
            for fn in os.listdir(extra_dir):
                if fn.lower().endswith(".xml"):
                    paths_to_try.append(os.path.join(extra_dir, fn))

    coords: dict[str, tuple[float, float, float]] = {}
    for p in paths_to_try:
        try:
            inv = read_inventory(p)
            for net in inv:
                for sta in net:
                    key = f"{net.code}.{sta.code}"
                    if key not in coords:
                        coords[key] = (sta.latitude or 0.0,
                                       sta.longitude or 0.0,
                                       (sta.elevation or 0.0) / 1000.0)
        except Exception as exc:
            print(f"[OnlineHypoDD] skip inventory {p}: {exc}", flush=True)
    return coords


def _write_station_dat(events: list[dict], sta_coords: dict,
                        work_dir: str) -> tuple[str, set[str]]:
    """Write the HypoDD station.dat from stations appearing in the events.
    Return (path, set_of_sta_keys_included)."""
    needed: set[str] = set()
    for ev in events:
        for p in (ev.get("p_picks") or []):
            key = f"{p.get('network','')}.{p.get('station','')}"
            needed.add(key)

    path = os.path.join(work_dir, "station.dat")
    written: set[str] = set()
    with open(path, "w") as f:
        for key in sorted(needed):
            if key not in sta_coords:
                continue
            lat, lon, elev = sta_coords[key]
            sta_code = key.split(".", 1)[-1]   # take only the STA code (without NET)
            f.write(f"{sta_code:<8}  {lat:9.4f}  {lon:10.4f}  {elev:6.3f}\n")
            written.add(key)
    return path, written


def _write_event_and_phase(events: list[dict], sta_written: set[str],
                             work_dir: str) -> tuple[str, str, dict[int, str]]:
    """Tulis event.dat dan phase.dat HypoDD.
    Return (event_path, phase_path, idx_to_event_id).

    HypoDD event.dat format (Waldhauser 2001):
      YR MO DY HR MN SC MAG LAT LON DEP EH EZ RMS ID

    phase.dat format:
      # ID ...event header...
      STA  TT_SEC  WEIGHT  PHASE
    """
    from obspy import UTCDateTime

    event_path = os.path.join(work_dir, "event.dat")
    phase_path = os.path.join(work_dir, "phase.dat")
    idx_to_eid: dict[int, str] = {}

    idx = 0
    with open(event_path, "w") as ef, open(phase_path, "w") as pf:
        for ev in events:
            picks = ev.get("p_picks") or []
            # only process events with at least 2 picks from available stations
            valid_picks = [
                p for p in picks
                if f"{p.get('network','')}.{p.get('station','')}" in sta_written
            ]
            if len(valid_picks) < 1:
                continue

            try:
                t0 = UTCDateTime(ev["datetime"])
            except Exception:
                continue

            mag  = ev.get("mag") or 0.0
            lat  = float(ev.get("lat", 0.0))
            lon  = float(ev.get("lon", 0.0))
            dep  = float(ev.get("depth_km", 10.0))
            rms  = float(ev.get("rms") or 0.0)

            sec = t0.second + t0.microsecond / 1e6

            # event.dat: format persis seperti catalog_picks_to_hypodd_phase
            ef.write(
                f"{t0.year:4d}{t0.month:02d}{t0.day:02d}  "
                f"{t0.hour:02d}{t0.minute:02d}{sec:5.2f}  "
                f"{lat:9.4f}  {lon:10.4f}  {dep:7.3f}  "
                f"{mag:5.2f}  0.00  0.00  0.00  {rms:.3f}  {idx:9d}\n"
            )

            # phase.dat header: spaces between each component (ph2dt format)
            pf.write(
                f"# {t0.year:4d} {t0.month:02d} {t0.day:02d} "
                f"{t0.hour:02d} {t0.minute:02d} {sec:5.2f} "
                f"{lat:8.4f} {lon:9.4f} "
                f"{dep:6.2f} {mag:4.1f} 0.00 0.00 0.00 {idx:9d}\n"
            )

            # phase lines: TT = pick_time - origin_time
            for p in valid_picks:
                sta_code = str(p.get("station", "")).strip()
                if not sta_code:
                    continue
                try:
                    tp = UTCDateTime(str(p["pick_time"]))
                    tt = tp - t0
                    if tt < 0 or tt > 300:  # skip odd picks
                        continue
                    pf.write(f"{sta_code:<8}  {tt:8.4f}  1.0  P\n")
                except Exception:
                    continue

            idx_to_eid[idx] = ev.get("event_id", str(idx))
            idx += 1

    return event_path, phase_path, idx_to_eid


def _parse_hypodd_reloc(reloc_file: str,
                         idx_to_eid: dict[int, str],
                         original_events: list[dict]) -> list[dict]:
    """Parse hypoDD.reloc → a list of events with updated coordinates.

    Format hypoDD.reloc (Waldhauser 2001):
      ID LAT LON DEP X Y Z EX EY EZ YR MO DY HR MN SC MAG NCCP NCCS NCTP NCTS
      RCC RCT CID
    """
    orig_by_id = {ev.get("event_id"): ev for ev in original_events}
    reloc: list[dict] = []
    if not os.path.exists(reloc_file):
        return reloc

    with open(reloc_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) < 17:
                continue
            try:
                ev_idx   = int(parts[0])
                lat_new  = float(parts[1])
                lon_new  = float(parts[2])
                dep_new  = float(parts[3])
                # Ex, Ey, Ez (horizontal/vertical error) di kolom 7-9
                ex = float(parts[6]) if len(parts) > 6 else None
                ez = float(parts[8]) if len(parts) > 8 else None
                nctp = int(parts[18]) if len(parts) > 18 else 0  # catalog P pairs used
            except (ValueError, IndexError):
                continue

            eid = idx_to_eid.get(ev_idx)
            if eid is None:
                continue

            # Merge with the original event
            orig = dict(orig_by_id.get(eid, {}))
            orig.update({
                "lat"          : lat_new,
                "lon"          : lon_new,
                "depth_km"     : dep_new,
                "reloc_method" : "HypoDD",
                "reloc_err_h"  : ex,
                "reloc_err_z"  : ez,
                "reloc_nobs"   : nctp,
            })
            reloc.append(orig)

    return reloc


# ── Main entry ────────────────────────────────────────────────────────────────

def run_online_hypodd(base_dir: str, inventory_path: str | None = None,
                      cfg_id: str | None = None):
    """Run HypoDD on the online catalog. Called from a background thread.

    The result is saved to <base_dir>/work/online_catalog/<cfg_id>/hypodd_reloc.json.
    Status can be polled via get_reloc_status().
    """
    run_id  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    work_dir = Path(base_dir) / "work" / "online_hypodd" / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    log_path = work_dir / "run.log"

    def _log(msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        with open(log_path, "a") as lf:
            lf.write(line + "\n")

    def _status(**kw):
        _set_status(cfg_id=cfg_id, **kw)

    _status(state="running", run_id=run_id,
            started=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            finished=None, n_input=0, n_reloc=0, error=None,
            log=str(log_path))

    try:
        # 1. Load online catalog
        from seiswork.web._realtime_pipeline import load_catalog, _catalog_path
        from seiswork.web._online_stations import find_default_inventory

        events = load_catalog(base_dir, cfg_id=cfg_id)
        events_with_picks = [e for e in events if e.get("p_picks")]
        n_total = len(events_with_picks)
        _log(f"Catalog: {len(events)} events, {n_total} with p_picks")
        _status(n_input=n_total)

        if n_total < 4:
            raise RuntimeError(f"Too few events with picks ({n_total} < 4)")

        # 2. Inventory → station coords
        inv_path = inventory_path
        if not inv_path or not os.path.exists(str(inv_path)):
            inv_path = find_default_inventory()
        if not inv_path:
            raise RuntimeError("Inventory XML not found")
        _log(f"Inventory: {inv_path}")
        sta_coords = _load_station_coords(str(inv_path))
        _log(f"Station coords loaded: {len(sta_coords)} stations")

        # 3. Tulis input files
        sta_path, sta_written = _write_station_dat(events_with_picks, sta_coords,
                                                    str(work_dir))
        _log(f"station.dat: {len(sta_written)} stations")

        ev_path, ph_path, idx_to_eid = _write_event_and_phase(
            events_with_picks, sta_written, str(work_dir))
        n_ph_events = len(idx_to_eid)
        _log(f"event.dat + phase.dat: {n_ph_events} events")
        _status(n_input=n_ph_events)

        if n_ph_events < 4:
            raise RuntimeError(f"After filtering picks: only {n_ph_events} events (<4)")

        # 4. ph2dt.inp
        ph2dt_inp = (
            f"* ph2dt.inp — online catalog HypoDD (SeisWork)\n"
            f"{os.path.basename(sta_path)}\n"
            f"{os.path.basename(ph_path)}\n"
            f"*MINWGHT  MAXDIST  MAXSEP  MAXNGH  MINLNK  MINOBS  MAXOBS\n"
            f"   0  300  150  10  2  2  64\n"
        )
        ph2dt_inp_path = work_dir / "ph2dt.inp"
        ph2dt_inp_path.write_text(ph2dt_inp)

        # 5. hypoDD.inp — IASP91 global model
        from seiswork.modules.relocation.hypodd import _VELOCITY_MODELS
        m = _VELOCITY_MODELS["iasp91"]
        tops_str = " ".join(str(t) for t in m["tops"])
        vels_str = " ".join(str(v) for v in m["vels"])
        nlay = len(m["tops"])
        vpvs = m["vpvs"]

        hypodd_inp = f"""\
* hypoDD.inp — online catalog (SeisWork, IASP91 global)
*
{os.path.basename(work_dir / 'dt.cc')}
*
dt.ct
*
event.dat
*
station.dat
*
hypoDD.loc
*
hypoDD.reloc
*
hypoDD.sta
*
hypoDD.res
*
hypoDD.src
*
*--- data type selection
* IDAT   IPHA   DIST
      2      1    500
*
*--- event clustering
* OBSCC  OBSCT
      0      2
*
*--- solution control
* ISTART  ISOLV  NSET
      1      2      4
*
*--- weighting
* NITER WTCCP WTCCS WRCC WDCC WTCTP WTCTS WRCT WDCT DAMP
      4    -9    -9   -9   -9   1.0   1.0  6.0 200  70
      4    -9    -9   -9   -9   1.0   0.9  4.0 150  70
      4    -9    -9   -9   -9   1.0   0.8  3.0 100  70
      4    -9    -9   -9   -9   1.0   0.8  2.0  80  70
*
*--- 1D model (IASP91 global)
* NLAY  RATIO
  {nlay:3d}  {vpvs:.3f}
* TOP
{tops_str}
* VEL
{vels_str}
*
*--- event selection
* CID
      0
* ID
"""
        hypodd_inp_path = work_dir / "hypoDD.inp"
        hypodd_inp_path.write_text(hypodd_inp)

        # 6. Jalankan ph2dt
        ph2dt_exec = _find_exec("ph2dt", base_dir)
        if not ph2dt_exec:
            raise RuntimeError("ph2dt binary not found")
        _log(f"ph2dt: {ph2dt_exec}")
        with open(log_path, "a") as lf:
            r = subprocess.run([ph2dt_exec, "ph2dt.inp"],
                               cwd=str(work_dir), stdout=lf, stderr=subprocess.STDOUT)
        dt_ct = work_dir / "dt.ct"
        if not dt_ct.exists() or dt_ct.stat().st_size == 0:
            raise RuntimeError("ph2dt failed: dt.ct is empty or missing")
        _log(f"ph2dt complete — dt.ct {dt_ct.stat().st_size} bytes")

        # 7. Jalankan hypoDD
        hypodd_exec = _find_exec("hypoDD", base_dir)
        if not hypodd_exec:
            raise RuntimeError("hypoDD binary not found")
        _log(f"hypoDD: {hypodd_exec}")
        with open(log_path, "a") as lf:
            r = subprocess.run([hypodd_exec, "hypoDD.inp"],
                               cwd=str(work_dir), stdout=lf, stderr=subprocess.STDOUT)
        reloc_file = str(work_dir / "hypoDD.reloc")
        if not os.path.exists(reloc_file):
            raise RuntimeError("hypoDD failed: hypoDD.reloc is missing")
        _log(f"hypoDD complete — reloc: {os.path.getsize(reloc_file)} bytes")

        # 8. Parse reloc → JSON
        reloc_events = _parse_hypodd_reloc(reloc_file, idx_to_eid, events_with_picks)
        n_reloc = len(reloc_events)
        _log(f"Relocation complete: {n_reloc} events from {n_ph_events} input")

        # Simpan ke online_catalog/<cfg_id>/hypodd_reloc.json
        _catalog_dir = Path(base_dir) / "work" / "online_catalog"
        if cfg_id:
            _catalog_dir = _catalog_dir / cfg_id
        _catalog_dir.mkdir(parents=True, exist_ok=True)
        out_path = _catalog_dir / "hypodd_reloc.json"
        out_path.write_text(json.dumps({
            "run_id"     : run_id,
            "run_at"     : datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "n_input"    : n_ph_events,
            "n_reloc"    : n_reloc,
            "vel_model"  : "IASP91 global",
            "events"     : reloc_events,
        }, default=str, indent=2))
        _log(f"Disimpan: {out_path}")

        _status(state="done",
                finished=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                n_input=n_ph_events, n_reloc=n_reloc,
                result=str(out_path))

    except Exception as exc:
        _log(f"ERROR: {exc}")
        _status(state="error",
                finished=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                error=str(exc))


def load_reloc_catalog(base_dir: str, cfg_id: str | None = None) -> dict | None:
    """Read the most recent relocation result. Return None when there isn't one yet."""
    cat_dir = Path(base_dir) / "work" / "online_catalog"
    if cfg_id:
        cat_dir = cat_dir / cfg_id
    path = cat_dir / "hypodd_reloc.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None

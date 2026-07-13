#!/usr/bin/env python3
"""
SeisWork - Parallel pipeline runner (per time chunk)
Author : HakimBMKG

What this does
--------------
Run the pipeline over long time ranges (e.g. 1 year) without running
out of memory. Each stage is parallelized based on what limits it:

  pick                 : GPU-bound, one pass over the whole range,
                         memory-safe (reads per station-day).
  associate/locate/mag : CPU-bound, run in PARALLEL per time chunk
                         (default 30 days) using ProcessPoolExecutor.
  relocate (HypoDD)    : needs many nearby events, so it runs ONCE
                         at the end on the merged catalog.

Why the old way crashed: old notebooks loaded a full year of waveforms
for all stations into RAM at once (~600 GB on 125 GB RAM). The picker
was fine; the orchestration was the problem.

Notes
-----
* ProcessPool uses "spawn": child processes are fresh and do not
  inherit the CUDA context, so no fork-after-CUDA crash.
* The ML module has its own Pool; each chunk worker forces
  `magnitude.workers = 1` to avoid nested-pool errors. Speed comes
  from running many chunks at once.
* Each chunk writes to its own folder (work/chunks/chunk_NN/...),
  so no file collisions.
* A failed or empty chunk does not stop the others.
"""

import os
import sys
import time
import shutil
import logging
import multiprocessing as mp
from copy import deepcopy
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

import pandas as pd

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
#  Worker — runs in a child process (must be module-level to be picklable/spawn)
# ════════════════════════════════════════════════════════════════════════════
def _process_chunk(job: dict) -> dict:
    """Associate -> locate -> magnitude for one time chunk.

    `job` holds only plain dict/str values so it can be pickled for spawn.
    Always returns a status dict; errors are caught inside.
    """
    t0 = time.time()
    label = job["label"]
    result = {"label": label, "status": "error", "n_assoc": 0,
              "n_loc": 0, "n_ml": 0, "catalog_ml": None, "error": None,
              "elapsed": 0.0}
    try:
        os.environ.setdefault("PYTHONNOUSERSITE", "1")
        os.environ.setdefault("MKL_THREADING_LAYER", "GNU")
        root = job["seiswork_root"]
        if root not in sys.path:
            sys.path.insert(0, root)

        from seiswork.modules.associator.gamma import GammaAssociator
        from seiswork.modules.locator.nlloc import NLLocLocator
        from seiswork.modules.magnitude.ml import MLMagnitude

        chunk_dir = Path(job["chunk_dir"])
        cat_dir   = chunk_dir / "catalog"
        log_dir   = chunk_dir / "logs"
        loc_dir   = chunk_dir / "location" / "nlloc"
        mag_dir   = chunk_dir / "magnitude"
        for d in (cat_dir, log_dir, loc_dir, mag_dir):
            d.mkdir(parents=True, exist_ok=True)

        picks_file = job["picks_file"]

        # ── 1) Associate (GaMMA) ──────────────────────────────────────────────
        assoc = GammaAssociator(job["assoc_cfg"], root)
        assoc.catalog_dir = cat_dir
        assoc.log_dir     = log_dir
        assoc.run(picks_file=picks_file)

        assoc_csv = cat_dir / "catalog_associated.csv"
        if not assoc_csv.exists() or pd.read_csv(assoc_csv).empty:
            result.update(status="empty", elapsed=time.time() - t0)
            return result
        result["n_assoc"] = len(pd.read_csv(assoc_csv))

        # ── 2) Locate (NonLinLoc) ─────────────────────────────────────────────
        locator = NLLocLocator(job["locate_cfg"], root)
        locator.out_dir     = loc_dir
        locator.catalog_dir = cat_dir
        locator.log_dir     = log_dir
        locator.run(catalog_file=str(assoc_csv))

        loc_csv = cat_dir / "catalog_located.csv"
        if not loc_csv.exists() or pd.read_csv(loc_csv).empty:
            result.update(status="located_empty", elapsed=time.time() - t0)
            return result
        result["n_loc"] = len(pd.read_csv(loc_csv))

        # ── 3) Magnitude (ML) — workers=1 to prevent nested daemonic pool ─────
        mag_cfg = deepcopy(job["mag_cfg"])
        mag_cfg.setdefault("magnitude", {})["workers"] = 1
        mag = MLMagnitude(mag_cfg, root)
        mag.out_dir     = mag_dir
        mag.catalog_dir = cat_dir
        mag.log_dir     = log_dir
        mag.run(catalog_file=str(loc_csv), inventory_file=job["inventory_file"])

        ml_csv = cat_dir / "catalog_ml.csv"
        if ml_csv.exists():
            result["n_ml"] = len(pd.read_csv(ml_csv))
            result["catalog_ml"] = str(ml_csv)
            result["status"] = "ok"
        else:
            result["status"] = "ml_missing"

    except Exception as e:                       # noqa: BLE001 — isolate per-chunk
        result["error"] = f"{type(e).__name__}: {e}"
        logger.exception("chunk %s failed", label)

    result["elapsed"] = time.time() - t0
    return result


# ════════════════════════════════════════════════════════════════════════════
#  Utility: split picks into per-chunk time windows
# ════════════════════════════════════════════════════════════════════════════
def _split_picks_by_time(picks_file: str, work_dir: Path,
                         t_start, t_end, chunk_days: int) -> list:
    """Split picks.csv → work_dir/chunks/chunk_NN/picks.csv per time window.

    Returns a list of dicts {label, chunk_dir, picks_file, n_picks}.
    Chunks with no picks are skipped.
    """
    from obspy import UTCDateTime

    df = pd.read_csv(picks_file)
    df["_t"] = pd.to_datetime(df["phase_time"], format="mixed")

    t0 = UTCDateTime(t_start)
    t1 = UTCDateTime(t_end)
    chunk_s = chunk_days * 86400

    chunks_root = work_dir / "chunks"
    if chunks_root.exists():
        shutil.rmtree(chunks_root)          # clean each run to avoid stale files
    chunks_root.mkdir(parents=True, exist_ok=True)

    out = []
    idx = 0
    t = t0
    while t < t1:
        t_next = min(UTCDateTime(float(t) + chunk_s), t1)
        lo = pd.Timestamp(t.datetime)
        hi = pd.Timestamp(t_next.datetime)
        sub = df[(df["_t"] >= lo) & (df["_t"] < hi)]
        if len(sub) > 0:
            idx += 1
            label = (f"chunk_{idx:03d}_{t.strftime('%Y%m%d')}"
                     f"_{t_next.strftime('%Y%m%d')}")
            cdir = chunks_root / label
            (cdir / "picks").mkdir(parents=True, exist_ok=True)
            pf = cdir / "picks" / "picks.csv"
            sub.drop(columns="_t").to_csv(pf, index=False)
            out.append({"label": label, "chunk_dir": str(cdir),
                        "picks_file": str(pf), "n_picks": len(sub)})
        t = t_next
    return out


def _default_workers(n_chunks: int) -> int:
    """Default worker count: conserve RAM, reserve cores for the OS. ~12 GB/worker assumed."""
    ncpu = os.cpu_count() or 4
    by_cpu = max(1, ncpu - 2)
    by_ram = by_cpu
    try:
        import psutil
        avail_gb = psutil.virtual_memory().available / 1e9
        by_ram = max(1, int(avail_gb // 12))
    except Exception:
        pass
    return max(1, min(by_cpu, by_ram, n_chunks, 8))


# ════════════════════════════════════════════════════════════════════════════
#  Main orchestrator
# ════════════════════════════════════════════════════════════════════════════
def run_parallel(*, seiswork_root: str, work_dir, t_start, t_end,
                 picker_cfg: dict, assoc_cfg: dict, locate_cfg: dict,
                 mag_cfg: dict, reloc_cfg: dict, inventory_file: str,
                 picks_file: str = None, chunk_days: int = 30,
                 n_workers: int = None, do_pick: bool = True,
                 do_relocate: bool = True) -> dict:
    """Run the full pipeline in parallel chunks. See module docstring for details.

    Returns a dict with paths to final artifacts and a summary.
    """
    work_dir = Path(work_dir)
    cat_out  = work_dir / "catalog"
    cat_out.mkdir(parents=True, exist_ok=True)
    t_all = time.time()

    print("=" * 70)
    print("  SeisWork — PARALLEL CHUNKED PIPELINE   (by HakimBMKG)")
    print("=" * 70)

    # ── Step 1: PICK (GPU, memory-bounded, single pass) ──────────────────────
    if do_pick:
        from seiswork.modules.picker.phasenet import PhaseNetPicker
        picker = PhaseNetPicker(picker_cfg, seiswork_root)
        picker.picks_dir = work_dir / "picks"
        picker.log_dir   = work_dir / "logs"
        picker.picks_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n[1/4] PICK (PhaseNet, GPU) — {t_start} → {t_end}")
        picks_file = picker.run()
        if not picks_file:
            print("[ABORT] Picker produced no picks.")
            return {"status": "no_picks"}
    else:
        picks_file = picks_file or str(work_dir / "picks" / "picks.csv")
        if not Path(picks_file).exists():
            raise FileNotFoundError(f"picks_file not found: {picks_file}")
        print(f"\n[1/4] PICK skipped — using {picks_file}")

    n_picks = len(pd.read_csv(picks_file))
    print(f"      picks.csv: {n_picks} picks")

    # ── Step 2: split into time chunks ───────────────────────────────────────
    chunks = _split_picks_by_time(picks_file, work_dir, t_start, t_end, chunk_days)
    if not chunks:
        print("[ABORT] No chunks contain any picks.")
        return {"status": "no_chunks", "picks_file": picks_file}
    n_workers = n_workers or _default_workers(len(chunks))
    print(f"\n[2/4] SPLIT → {len(chunks)} chunks × {chunk_days} days  "
          f"| parallel {n_workers} workers (spawn)")

    # ── Step 3: associate + locate + magnitude PARALLEL per chunk ─────────────
    jobs = []
    for c in chunks:
        jobs.append({
            "seiswork_root": seiswork_root,
            "chunk_dir":     c["chunk_dir"],
            "picks_file":    c["picks_file"],
            "assoc_cfg":     assoc_cfg,
            "locate_cfg":    locate_cfg,
            "mag_cfg":       mag_cfg,
            "inventory_file": inventory_file,
            "label":         c["label"],
        })

    print(f"\n[3/4] ASSOCIATE → LOCATE → MAGNITUDE (parallel)")
    ctx = mp.get_context("spawn")
    results = []
    done = 0
    with ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx) as pool:
        futs = {pool.submit(_process_chunk, j): j["label"] for j in jobs}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            done += 1
            tag = {"ok": "✓", "empty": "∅", "located_empty": "∅",
                   "ml_missing": "?", "error": "✗"}.get(r["status"], "?")
            msg = (f"      [{done}/{len(jobs)}] {tag} {r['label']}  "
                   f"assoc={r['n_assoc']} loc={r['n_loc']} ml={r['n_ml']}  "
                   f"{r['elapsed']:.0f}s")
            if r["status"] == "error":
                msg += f"  ERR: {r['error']}"
            print(msg)

    ok = [r for r in results if r["status"] == "ok"]
    print(f"\n      Chunks succeeded: {len(ok)}/{len(jobs)}")

    # ── Merge ML catalogs from all chunks ────────────────────────────────────
    frames = []
    for r in ok:
        try:
            frames.append(pd.read_csv(r["catalog_ml"]))
        except Exception as e:
            logger.warning("failed to read %s: %s", r["catalog_ml"], e)
    if not frames:
        print("[ABORT] No ML catalog to merge.")
        return {"status": "no_catalog", "results": results}

    merged = pd.concat(frames, ignore_index=True)
    if "datetime" in merged.columns:
        merged["datetime"] = pd.to_datetime(merged["datetime"], format="mixed")
        merged.sort_values("datetime", inplace=True)
    merged.reset_index(drop=True, inplace=True)
    merged_ml = cat_out / "catalog_ml.csv"
    merged.to_csv(merged_ml, index=False)
    print(f"      catalog_ml.csv merged: {len(merged)} events → {merged_ml}")

    # Merge associated/located catalogs for downstream plotting
    for name in ("catalog_associated.csv", "catalog_located.csv"):
        fr = []
        for r in ok:
            f = Path(r["catalog_ml"]).parent / name
            if f.exists():
                try:
                    fr.append(pd.read_csv(f))
                except Exception:
                    pass
        if fr:
            mdf = pd.concat(fr, ignore_index=True)
            if "datetime" in mdf.columns:
                mdf["datetime"] = pd.to_datetime(mdf["datetime"], format="mixed")
                mdf.sort_values("datetime", inplace=True)
            mdf.to_csv(cat_out / name, index=False)

    # ── Step 4: RELOCATE once over the merged catalog ─────────────────────────
    reloc_csv = None
    if do_relocate:
        print(f"\n[4/4] RELOCATE (HypoDD, merged catalog {len(merged)} events)")
        try:
            from seiswork.modules.relocation.hypodd import HypoDDRelocation
            reloc = HypoDDRelocation(reloc_cfg, seiswork_root)
            reloc.run_catalog(catalog_file=str(merged_ml))
            src = Path(seiswork_root) / "work" / "catalog" / "catalog_relocated.csv"
            if src.exists():
                dst = work_dir / "relocation" / "catalog" / "catalog_reloc.csv"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, dst)
                reloc_csv = str(dst)
                print(f"      catalog_reloc.csv: {len(pd.read_csv(dst))} events → {dst}")
        except SystemExit:
            print("      [WARN] HypoDD binary not found — relocation skipped.")
        except Exception as e:                   # noqa: BLE001
            print(f"      [WARN] HypoDD failed: {type(e).__name__}: {e}")
    else:
        print("\n[4/4] RELOCATE skipped (do_relocate=False)")

    elapsed = time.time() - t_all
    print("\n" + "=" * 70)
    print(f"  DONE in {elapsed/60:.1f} minutes")
    print(f"    picks      : {n_picks}")
    print(f"    chunks ok  : {len(ok)}/{len(jobs)}")
    print(f"    events ML  : {len(merged)}")
    print(f"    output     : {work_dir}")
    print("=" * 70)

    return {
        "status": "ok",
        "picks_file": picks_file,
        "catalog_ml": str(merged_ml),
        "catalog_reloc": reloc_csv,
        "n_picks": n_picks,
        "n_events": len(merged),
        "chunks": results,
        "elapsed_min": elapsed / 60,
    }

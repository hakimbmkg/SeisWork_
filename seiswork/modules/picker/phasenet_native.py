"""
PhaseNet NATIVE picker for SeisWork — by HakimBMKG

Drives the original AI4EPS PhaseNet (TensorFlow predict.py) instead of the
seisbench port:

    SDS scan -> fname.csv (station-day globs) -> split into N shards
             -> N predict.py workers (saturate the GPU)
             -> convert + merge native picks.csv -> SeisWork picks.csv

Why native: RAM-light (one station-day at a time, no OOM), GPU-saturating
(N independent TF processes), zero data copy (fname entries are SDS globs),
and fast (~4 h/year at 10 workers vs ~9-13 h for the seisbench path).

Resume: each shard writes out_<i>/picks.csv + a .done marker; re-running the
same job only re-picks the missing shards.
"""

import os
import re
import sys
import csv
import time
import logging
import threading
import subprocess
from pathlib import Path

import pandas as pd

logger = logging.getLogger("phasenet_native")

# Default AI4EPS PhaseNet repo + model (cloned by install.sh). Override via
# config pick.phasenet_native.* or the PHASENET_DIR / PHASENET_MODEL envs.
_DEFAULT_PN_DIR  = os.environ.get(
    "PHASENET_DIR", os.path.join(os.path.expanduser("~"), "apps", "PhaseNet"))
_DEFAULT_MODEL   = os.environ.get(
    "PHASENET_MODEL", os.path.join(_DEFAULT_PN_DIR, "model", "190703-214543"))


class PhaseNetNativePicker:
    """Native PhaseNet (TensorFlow) picker — fname.csv sharding over the GPU."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = base_dir
        self.pcfg     = cfg.get("pick", {}).get("phasenet_native", {})

        _pd = cfg["data"].get("picks_dir", "")
        self.picks_dir = Path(_pd) if _pd else Path(base_dir) / "work" / "picks"
        self.picks_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir  = self.picks_dir / "_pn_native"   # fname lists + shard outputs
        self.work_dir.mkdir(parents=True, exist_ok=True)

        # engine / env
        self.pn_dir    = Path(self.pcfg.get("phasenet_dir", _DEFAULT_PN_DIR))
        self.model_dir = self.pcfg.get("model_dir", _DEFAULT_MODEL)
        self.predict_py = str(self.pn_dir / "phasenet" / "predict.py")

        # picking params
        self.workers   = int(self.pcfg.get("workers", 10))   # parallel predict.py procs
        self.highpass  = float(self.pcfg.get("highpass_hz", 1.0))
        self.p_thresh  = float(self.pcfg.get("p_threshold", 0.3))
        self.s_thresh  = float(self.pcfg.get("s_threshold", 0.3))
        self.batch_size = int(self.pcfg.get("batch_size", 1))
        self.amplitude  = bool(self.pcfg.get("amplitude", True))

        # Docker only: predict.py runs inside the TF-GPU image (Hopper-compatible
        # cuDNN), isolated from host conda envs. No conda fallback.
        self.docker_image = self.pcfg.get("docker_image", "seiswork/phasenet:tf2.12")

        # process label for the system monitor
        self.job_label = self.picks_dir.name or "picks"
        # delay between GPU worker launches so their first big cuDNN convs
        # don't spike GPU memory at the same time
        self.stagger_s = float(self.pcfg.get("stagger_s", 10))
        # skip SDS day files larger than this (corrupt >24h files crash cuDNN);
        # 0 disables. A normal 200 Hz day is ~38 MB.
        self.max_day_bytes = float(self.pcfg.get("max_day_mb", 50)) * 1e6

        # SDS source
        self.sds_path  = Path(self.pcfg.get("sds_path")
                              or cfg["data"].get("waveform_dir", ""))
        self.network   = cfg["data"].get("network", "*")

    # =====================================================================
    # 1. Build the per-station-day task list (SDS globs) — no data copy
    # =====================================================================
    @staticmethod
    def _is_float(s):
        try:
            float(s); return True
        except (TypeError, ValueError):
            return False

    def _load_allowed_stations(self) -> set:
        """(net, sta) pairs picking is restricted to, from the config's
        station_file. Empty set = no restriction. Station-only rows match
        any network via ('*', sta)."""
        sf = (self.cfg.get("data", {}) or {}).get("station_file", "")
        if not sf:
            return set()
        p = Path(sf)
        if not p.is_absolute():
            p = Path(self.base_dir) / sf
        if not p.exists():
            return set()
        allowed = set()
        try:
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = re.split(r"[|\s,]+", line)
                if len(parts) >= 2 and not self._is_float(parts[1]):
                    allowed.add((parts[0].strip(), parts[1].strip()))   # net, sta
                elif parts:
                    allowed.add(("*", parts[0].strip()))                # sta only
        except Exception as e:                                          # noqa: BLE001
            logger.warning("station_file read failed (%s): %s", sf, e)
        return allowed

    def _collect_station_days(self) -> list:
        """Scan SDS -> one (glob, size_bytes) per station-day with a Z file in
        the time range. The glob expands to all 3 components inside predict.py;
        size_bytes is used to balance the shards."""
        from obspy import UTCDateTime
        t_start = UTCDateTime(self.cfg["region"]["starttime"])
        t_end   = UTCDateTime(self.cfg["region"]["endtime"])
        req_years = set(range(t_start.year, t_end.year + 1))

        # Restrict to the config's selected stations. Empty = no restriction.
        allowed = self._load_allowed_stations()
        if allowed:
            print(f"[PN-native] station filter: {len(allowed)} selected station(s) "
                  f"from config station_file", flush=True)

        tasks = []
        n_skipped = 0
        for year_dir in sorted(self.sds_path.iterdir()):
            if not (year_dir.is_dir() and year_dir.name.isdigit()):
                continue
            year = int(year_dir.name)
            if year not in req_years:
                continue
            net_pat = self.network if self.network != "*" else "*"
            for net_dir in sorted(year_dir.glob(net_pat)):
                if not net_dir.is_dir():
                    continue
                for sta_dir in sorted(net_dir.iterdir()):
                    if not sta_dir.is_dir():
                        continue
                    # only the config-selected stations (net,sta) or (*,sta)
                    if allowed and (net_dir.name, sta_dir.name) not in allowed \
                            and ("*", sta_dir.name) not in allowed:
                        continue
                    # vertical-component channel dir → defines the band (e.g. "HH")
                    z_dirs = sorted(sta_dir.glob("*Z.D"))
                    if not z_dirs:
                        continue
                    z_dir = z_dirs[0]
                    cha   = z_dir.name[:-2]        # "HHZ.D" → "HHZ"
                    band  = cha[:-1]               # "HHZ"   → "HH"
                    for f in sorted(z_dir.iterdir()):
                        # filename: NET.STA.LOC.CHA.D.YEAR.JDAY
                        parts = f.name.split(".")
                        if len(parts) < 7:
                            continue
                        try:
                            fyear, jday = int(parts[-2]), int(parts[-1])
                        except ValueError:
                            continue
                        # keep only day files inside [t_start, t_end]
                        day = UTCDateTime(year=fyear, julday=jday)
                        if day + 86400 <= t_start or day >= t_end:
                            continue
                        loc = parts[2]
                        glob = (f"{self.sds_path}/{year}/{net_dir.name}/"
                                f"{sta_dir.name}/{band}?.D/"
                                f"{net_dir.name}.{sta_dir.name}.{loc}.{band}?.D."
                                f"{fyear}.{parts[-1]}")
                        try:
                            size = f.stat().st_size
                        except OSError:
                            size = 0
                        # Skip corrupt day files holding much more than 24 h —
                        # their giant tensors crash cuDNN and kill the shard.
                        if self.max_day_bytes and size > self.max_day_bytes:
                            logger.warning("skip oversized/corrupt day (%.0f MB): %s",
                                           size / 1e6, glob)
                            n_skipped += 1
                            continue
                        tasks.append((glob, size))
        if n_skipped:
            print(f"[PN-native] skipped {n_skipped} oversized/corrupt day file(s) "
                  f"(> {self.max_day_bytes/1e6:.0f} MB, >24h)", flush=True)
        return tasks

    @staticmethod
    def _balance_shards(tasks: list, n: int) -> list:
        """Greedy bin-packing: split tasks into n shards of ~equal total bytes,
        so all GPU workers finish around the same time."""
        shards      = [[] for _ in range(n)]
        shard_bytes = [0] * n
        for glob, size in sorted(tasks, key=lambda t: t[1], reverse=True):
            j = min(range(n), key=lambda k: shard_bytes[k])  # emptiest shard
            shards[j].append(glob)
            shard_bytes[j] += size
        return shards, shard_bytes

    # =====================================================================
    # 2. Run: shard the list, launch predict.py workers, merge
    # =====================================================================
    def run(self, workers: int = None):
        n_w = int(workers or self.workers)
        self._label_process()       # tag this PID in top/htop with the method

        if not self.predict_py or not Path(self.predict_py).exists():
            print(f"[PN-native] ERROR: predict.py not found at {self.predict_py}")
            sys.exit(1)

        tasks = self._collect_station_days()
        if not tasks:
            print(f"[PN-native] ERROR: no SDS station-days found "
                  f"(path={self.sds_path}, range={self.cfg['region']['starttime']}"
                  f"..{self.cfg['region']['endtime']})")
            sys.exit(1)

        n_w = max(1, min(n_w, len(tasks)))
        # Balance shards by data volume so all GPU workers finish together.
        shards, shard_bytes = self._balance_shards(tasks, n_w)
        total_gb = sum(shard_bytes) / 1e9
        print(f"[PN-native] {len(tasks)} station-days ({total_gb:.1f} GB) → "
              f"{n_w} balanced shard(s), engine=docker {self.docker_image}", flush=True)
        for i in range(n_w):
            print(f"[PN-native]   shard {i}: {len(shards[i])} days, "
                  f"{shard_bytes[i]/1e9:.2f} GB", flush=True)

        # write one fname.csv per shard
        for i, shard in enumerate(shards):
            self._write_fname_csv(self.work_dir / f"fname_{i}.csv", shard)

        # ── RESUME: skip shards already completed ────────────────────────
        def _done(i):
            return (self.work_dir / f"out_{i}" / ".done").exists()
        todo = [i for i in range(n_w) if not _done(i)]
        skip = [i for i in range(n_w) if _done(i)]
        if skip:
            print(f"[PN-native] RESUME: shard(s) {skip} already done "
                  f"→ running only {todo}", flush=True)

        t0 = time.time()
        procs = {}
        for k, i in enumerate(todo):
            out_dir = self.work_dir / f"out_{i}"
            out_dir.mkdir(parents=True, exist_ok=True)
            # Stagger startups: simultaneous first convs spike cuDNN workspace
            # memory and can kill a worker.
            if k > 0:
                time.sleep(self.stagger_s)
            procs[i] = self._launch_worker(i, self.work_dir / f"fname_{i}.csv",
                                           out_dir)
            print(f"[PN-native]   worker {i}: {len(shards[i])} station-days "
                  f"(pid {procs[i].pid})", flush=True)

        # live progress → runner stdout (GUI parses 'read=N/TOTAL' for the bar)
        stop_prog = threading.Event()
        prog_thr  = threading.Thread(target=self._progress_monitor,
                                     args=(todo, shards, stop_prog), daemon=True)
        prog_thr.start()

        failed = []
        for i, pr in procs.items():
            rc = pr.wait()
            out_dir = self.work_dir / f"out_{i}"
            if rc == 0:
                # predict.py only writes picks.csv when picks > 0; mark done anyway
                (out_dir / ".done").touch()
                print(f"[PN-native]   worker {i}: done (rc=0)", flush=True)
            else:
                failed.append((i, rc, len(shards[i])))
        stop_prog.set()   # stop progress monitor once all workers joined

        # ── auto-retry failed shards sequentially (solo = full GPU) ─────────
        # Parallel workers can die when big convs contend for GPU workspace.
        # Solo re-runs get the whole GPU; only shards that fail solo are fatal.
        if failed:
            retry, failed = failed, []
            print(f"[PN-native] {len(retry)} shard(s) failed in parallel — "
                  f"retrying sequentially (solo, full GPU) ...", flush=True)
            for i, _rc, n in retry:
                out_dir = self.work_dir / f"out_{i}"
                pr = self._launch_worker(i, self.work_dir / f"fname_{i}.csv", out_dir)
                print(f"[PN-native]   retry shard {i}: {n} station-days "
                      f"(pid {pr.pid}, solo)", flush=True)
                rc2 = pr.wait()
                if rc2 == 0:
                    (out_dir / ".done").touch()
                    print(f"[PN-native]   retry shard {i}: done (rc=0)", flush=True)
                else:
                    failed.append((i, rc2, n))

        if failed:
            lost = sum(n for _, _, n in failed)
            bar = "=" * 60
            print(f"[PN-native] {bar}", flush=True)
            print(f"[PN-native] FATAL: {len(failed)}/{len(todo)} worker(s) failed "
                  f"→ {lost} station-day(s) UNPICKED:", flush=True)
            for i, rc, n in failed:
                print(f"[PN-native]   worker {i}: rc={rc} — {n} station-day(s) "
                      f"LOST (see {self.work_dir}/out_{i}/worker.log)", flush=True)
            print(f"[PN-native] Refusing to merge a PARTIAL catalog. Re-run this "
                  f"same job — RESUME re-picks ONLY the failed shard(s).", flush=True)
            print(f"[PN-native] {bar}", flush=True)
            raise RuntimeError(f"{len(failed)} PhaseNet worker(s) failed — "
                               f"{lost} station-days unpicked")

        # ── convert + merge every shard's native picks.csv ──────────────
        out = self._merge_and_convert(n_w)
        if out is None:
            print("[PN-native] No picks produced by any worker.")
            return None

        elapsed = time.time() - t0
        df = pd.read_csv(out)
        p = int((df["phase_hint"] == "P").sum())
        s = int((df["phase_hint"] == "S").sum())
        print(f"[PN-native] Done. {len(df)} picks (P={p}, S={s}) → {out}  "
              f"({elapsed:.1f}s total)", flush=True)
        return str(out)

    # =====================================================================
    # helpers
    # =====================================================================
    def _label_process(self):
        """Label this PID in top/htop via /proc/self/comm (max 15 chars).
        Workers are labelled separately via docker --name."""
        try:
            with open("/proc/self/comm", "w") as fh:
                fh.write("sw-pn_native")     # ≤15 chars; shows the method
        except Exception:
            pass

    @staticmethod
    def _write_fname_csv(path: Path, globs: list):
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["fname"])
            for g in globs:
                w.writerow([g])

    @staticmethod
    def _read_shard_progress(worker_log: Path):
        """Latest (done, total) from a predict.py worker.log tqdm line
        (e.g. 'Pred:  58%|...| 1962/3383 [..]')."""
        try:
            with open(worker_log, "rb") as fh:
                fh.seek(0, 2); size = fh.tell()
                fh.seek(max(0, size - 4096))
                tail = fh.read().decode("utf-8", "ignore").replace("\r", "\n")
        except Exception:
            return (0, 0)
        last = None
        for m in re.finditer(r"(\d+)/(\d+)\s*\[", tail):
            last = m
        return (int(last.group(1)), int(last.group(2))) if last else (0, 0)

    def _progress_monitor(self, todo, shards, stop_event, interval=25):
        """Daemon thread: aggregate each shard's worker.log progress and print
        a 'read=N/TOTAL' line the GUI turns into a progress bar."""
        total = sum(len(shards[i]) for i in todo) or 1
        while not stop_event.is_set():
            done = sum(self._read_shard_progress(self.work_dir / f"out_{i}" / "worker.log")[0]
                       for i in todo)
            pct = round(done / total * 100, 1)
            per = " ".join(
                f"s{i}={self._read_shard_progress(self.work_dir / f'out_{i}' / 'worker.log')[0]}"
                for i in todo)
            print(f"[PN-native] progress read={done}/{total} ({pct}%)  [{per}]",
                  flush=True)
            stop_event.wait(interval)

    def _predict_args(self, fname_csv, result_dir) -> list:
        """predict.py CLI args (paths identical inside the container — dirs are
        bind-mounted at their host paths)."""
        args = [
            f"--model_dir={self.model_dir}",
            "--data_dir=",                       # fname entries are absolute globs
            f"--data_list={fname_csv}",
            f"--result_dir={result_dir}",
            "--result_fname=picks",
            "--format=mseed",
            f"--batch_size={self.batch_size}",
            f"--highpass_filter={self.highpass}",
            f"--min_p_prob={self.p_thresh}",
            f"--min_s_prob={self.s_thresh}",
        ]
        if self.amplitude:
            args.append("--amplitude")
        return args

    def _launch_worker(self, idx: int, fname_csv: Path, out_dir: Path):
        """Spawn one predict.py worker inside the TF-GPU container. Bind-mounts
        keep host paths unchanged inside; --user keeps outputs host-owned."""
        log_fh = open(out_dir / "worker.log", "w")
        args   = self._predict_args(fname_csv, out_dir)
        uid, gid = os.getuid(), os.getgid()
        # Persistent CUDA JIT cache: TF 2.12 JIT-compiles kernels on first use
        # (~34 s); a shared cache lets later workers reuse them.
        cache_dir = self.work_dir / "nv_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        # container name shows method+job+shard in docker ps and host ps
        cname = f"sw-pick-phasenet_native-{self.job_label}-w{idx}"
        cmd = [
            "docker", "run", "--rm", "--name", cname, "--gpus", "all",
            "--user", f"{uid}:{gid}",
            "-e", "TF_FORCE_GPU_ALLOW_GROWTH=true",
            "-e", "TF_CPP_MIN_LOG_LEVEL=3",
            "-e", "HOME=/cache",
            "-v", f"{cache_dir}:/cache",
            "-v", f"{self.pn_dir}:{self.pn_dir}:ro",
            "-v", f"{self.sds_path}:{self.sds_path}:ro",
            "-v", f"{self.picks_dir}:{self.picks_dir}",
            "-w", str(self.pn_dir),
            self.docker_image,
            "python", self.predict_py, *args,
        ]
        return subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT)

    def _merge_and_convert(self, n_w: int):
        """Read each shard's native picks.csv, map to the SeisWork schema, merge."""
        frames = []
        for i in range(n_w):
            f = self.work_dir / f"out_{i}" / "picks.csv"
            if not f.exists():
                continue          # shard produced zero picks (ok)
            try:
                d = pd.read_csv(f)
            except Exception:
                continue
            if not d.empty:
                frames.append(self._to_seiswork_schema(d))
        if not frames:
            return None
        result = pd.concat(frames, ignore_index=True)
        result.sort_values("phase_time", inplace=True)
        result.reset_index(drop=True, inplace=True)
        out = self.picks_dir / "picks.csv"
        result.to_csv(out, index=False)
        return out

    @staticmethod
    def _to_seiswork_schema(d: pd.DataFrame) -> pd.DataFrame:
        """Native PhaseNet picks.csv → SeisWork picks.csv columns.

        Native: station_id(NET.STA.LOC.BB), begin_time, phase_index, phase_time,
                phase_score, phase_type, file_name, phase_amplitude, phase_amp
        SeisWork: network, station, location, channel, station_id, phase_hint,
                  phase_time, phase_score, phase_amp, method
        """
        sid = d["station_id"].astype(str).str.split(".", expand=True)
        # pad to 4 columns: net, sta, loc, band
        for c in range(4):
            if c not in sid.columns:
                sid[c] = ""
        net  = sid[0].fillna("")
        sta  = sid[1].fillna("")
        loc  = sid[2].fillna("")
        band = sid[3].fillna("")

        out = pd.DataFrame({
            "network"    : net,
            "station"    : sta,
            "location"   : loc,
            "channel"    : band + "Z",
            "station_id" : d["station_id"].astype(str),
            "phase_hint" : d["phase_type"].astype(str),
            "phase_time" : d["phase_time"].astype(str),
            "phase_score": pd.to_numeric(d["phase_score"], errors="coerce").round(4),
            "phase_amp"  : pd.to_numeric(
                d["phase_amp"] if "phase_amp" in d.columns else
                d.get("phase_amplitude"), errors="coerce"),
            "method"     : "phasenet_native",
        })
        return out

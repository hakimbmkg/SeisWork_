#!/usr/bin/env python3
"""
SeisWork picking subprocess runner - by HakimBMKG

Run by Flask's _pick_worker() as a separate subprocess.
The if __name__ == "__main__" guard is required: without it, multiprocessing
spawn mode would re-execute this file in every worker, causing recursive
spawns and a "bootstrapping phase" RuntimeError.
"""
import sys
import traceback
from pathlib import Path


if __name__ == "__main__":
    cfg_yaml = sys.argv[1]
    base_dir = Path(sys.argv[2])
    method   = sys.argv[3]
    job_id   = sys.argv[4]

    print(f"[runner] base_dir={base_dir}", flush=True)
    print(f"[runner] method={method}", flush=True)
    print(f"[runner] config={cfg_yaml}", flush=True)

    import yaml
    with open(cfg_yaml) as fh:
        cfg = yaml.safe_load(fh)

    try:
        if method == "phasenet":
            from seiswork.modules.picker.phasenet import PhaseNetPicker
            workers = cfg.get("pick", {}).get("phasenet", {}).get("workers", 4)
            PhaseNetPicker(cfg, base_dir).run(workers=workers)
        elif method == "phasenet_native":
            from seiswork.modules.picker.phasenet_native import PhaseNetNativePicker
            workers = cfg.get("pick", {}).get("phasenet_native", {}).get("workers", 10)
            PhaseNetNativePicker(cfg, base_dir).run(workers=workers)
        elif method == "eqt":
            from seiswork.modules.picker.eqt import EQTPicker
            workers = cfg.get("pick", {}).get("eqt", {}).get("workers", 4)
            EQTPicker(cfg, base_dir).run(workers=workers)
        elif method == "stalta":
            from seiswork.modules.picker.stalta import STALTAPicker
            STALTAPicker(cfg, base_dir).run()
        else:
            print(f"[runner] ERROR: unknown method: {method}", flush=True)
            sys.exit(1)
        print(f"[runner] DONE: picking {method} completed.", flush=True)
    except Exception:
        print("[runner] ERROR: exception in picker", flush=True)
        traceback.print_exc()
        sys.exit(2)

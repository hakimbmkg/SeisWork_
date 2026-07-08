#!/usr/bin/env python3
"""
SeisWork DeepDenoiser subprocess runner — by HakimBMKG

Dipanggil oleh Flask _denoise_worker() sebagai subprocess terpisah.
"""
import sys
import traceback
from pathlib import Path

if __name__ == "__main__":
    cfg_yaml = sys.argv[1]
    base_dir = Path(sys.argv[2])
    job_id   = sys.argv[3]

    print(f"[denoise-runner] base_dir={base_dir}", flush=True)
    print(f"[denoise-runner] config={cfg_yaml}", flush=True)

    import yaml
    with open(cfg_yaml) as fh:
        cfg = yaml.safe_load(fh)

    try:
        from seiswork.modules.denoiser.deepdenoiser_standalone import DeepDenoiserRunner
        runner = DeepDenoiserRunner(cfg, base_dir)
        out_dir = runner.run()
        print(f"[denoise-runner] DONE: denoised waveforms di {out_dir}", flush=True)
    except Exception:
        print("[denoise-runner] ERROR:", flush=True)
        traceback.print_exc()
        sys.exit(2)

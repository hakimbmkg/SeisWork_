#!/usr/bin/env python3
"""
SeisWork full-pipeline orchestrator - by HakimBMKG

Chains the post-pick pipeline on a picks.csv by calling the local GUI
server's /api/pipeline/run endpoint, feeding each step's output catalog
into the next:

    picks.csv -> assoc/gamma -> locate/nlloc -> magnitude/ml -> relocation/hypodd

Each step's status.json carries state + result_file; we poll state and
chain on result_file (no filename guessing). Auth uses the bearer token
at ~/.seiswork/token.

Usage:
  python _full_pipeline.py <picks_csv> <cfg_id> [base_url]
"""
import sys
import json
import time
import urllib.request
from pathlib import Path

BASE_URL = "http://127.0.0.1:5000"
PIPE_DIR = Path(__file__).resolve().parent / "tmp" / "pipeline"
TOKEN    = ""
try:
    TOKEN = (Path.home() / ".seiswork" / "token").read_text().strip()
except Exception:
    TOKEN = ""

# step -> (method, params). assoc uses GaMMA DBSCAN pre-clustering so it scales
# to a full year of picks (millions) in one pass: DBSCAN clusters by space-time
# first, then BGMM associates within each cluster. Other steps use GUI defaults.
_ASSOC_DBSCAN = {
    "use_dbscan"        : True,
    "dbscan_eps"        : 10.0,    # seconds, time radius for space-time clusters
    "dbscan_min_samples": 4,
    "dbscan_min_cluster": 50,      # min picks to keep a cluster
    "oversample"        : 5,
    "min_picks"         : 5,
    "min_p"             : 1,
    "min_s"             : 0,
    "use_amp"           : True,
}
PIPELINE = [
    ("assoc",      "gamma",  _ASSOC_DBSCAN),
    ("locate",     "nlloc",  {}),
    ("magnitude",  "ml",     {}),
    ("relocation", "hypodd", {}),
]


def _api_post(path, payload):
    req = urllib.request.Request(
        BASE_URL + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _job_status(job_id):
    sf = PIPE_DIR / job_id / "status.json"
    if sf.exists():
        try:
            return json.loads(sf.read_text())
        except Exception:
            pass
    return {}


def run_step(step, method, cfg_id, input_file, params=None):
    r = _api_post("/api/pipeline/run",
                  {"cfg_id": cfg_id, "step": step, "method": method,
                   "params": params or {}, "input_file": input_file})
    job_id = r.get("id")
    print(f"[full-pipe] {step}/{method} job {job_id} ← {input_file}", flush=True)
    if not job_id:
        raise SystemExit(f"[full-pipe] {step} did not start: {r}")
    # poll status.json
    t0 = time.time()
    while True:
        st = _job_status(job_id)
        state = st.get("state", "running")
        if state in ("done", "error", "stopped"):
            break
        time.sleep(15)
    elapsed = int(time.time() - t0)
    out = st.get("result_file", "")
    ev  = st.get("events", "?")
    print(f"[full-pipe] {step}/{method} → {state} in {elapsed}s "
          f"(events={ev}) out={out}", flush=True)
    if state != "done" or not out or not Path(out).exists():
        raise SystemExit(f"[full-pipe] FAILED at {step}/{method} "
                         f"(state={state}, out={out})")
    return out


def main():
    if len(sys.argv) < 3:
        print("usage: _full_pipeline.py <picks_csv> <cfg_id> [base_url]")
        sys.exit(1)
    picks  = sys.argv[1]
    cfg_id = sys.argv[2]
    global BASE_URL
    if len(sys.argv) > 3:
        BASE_URL = sys.argv[3]
    if not Path(picks).exists():
        print(f"[full-pipe] picks not found: {picks}")
        sys.exit(1)

    print(f"[full-pipe] START picks={picks} cfg_id={cfg_id}", flush=True)
    cur = picks
    for step, method, params in PIPELINE:
        cur = run_step(step, method, cfg_id, cur, params)
    print(f"[full-pipe] ✅ DONE — final relocated catalog: {cur}", flush=True)


if __name__ == "__main__":
    main()

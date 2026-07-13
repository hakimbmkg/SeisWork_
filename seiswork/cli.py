"""
SeisWork — Simple Seismological Data Processing Framework
Author  : HakimBMKG
Version : 0.0.1(BETA)

Pipeline:
  waveforms → pick → associate → velocity → locate → magnitude → relocate
"""

import os
import sys
import argparse
import textwrap
from pathlib import Path
import yaml

# ── Environment setup (GPU + CUDA) ────────────────────────────────────────────
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
os.environ.setdefault("TF_XLA_FLAGS", "--tf_xla_auto_jit=0")
os.environ.setdefault("TF_XLA_ENABLE_XLA_DEVICES", "0")
os.environ.setdefault("TF_DISABLE_CUDNN_AUTOTUNE", "1")
os.environ["TF_FORCE_GPU_ALLOW_GROWTH"] = "true"
conda_prefix = os.environ.get("CONDA_PREFIX", "")
if conda_prefix:
    lib_path = os.path.join(conda_prefix, "lib")
    os.environ["LD_LIBRARY_PATH"] = lib_path + ":" + os.environ.get("LD_LIBRARY_PATH", "")

# BASE_DIR = project root, one level above this package (config/, core/, work/ live there)
BASE_DIR  = Path(__file__).resolve().parent.parent
WORK_DIR  = BASE_DIR / "work"
CFG_FILE  = BASE_DIR / "config" / "config.yaml"

# Add core/bin to PATH so compiled tools are found without a system install
_core_bin = BASE_DIR / "core" / "bin"
if _core_bin.is_dir():
    os.environ["PATH"] = str(_core_bin) + ":" + os.environ.get("PATH", "")

# ── Banner ─────────────────────────────────────────────────────────────────────
BANNER = r"""
  ____       _     __        __         _    
 / ___|  ___(_)___ \ \      / /__  _ __| | __
 \___ \ / _ \ / __| \ \ /\ / / _ \| '__| |/ /
  ___) |  __/ \__ \  \ V  V / (_) | |  |   < 
 |____/ \___|_|___/   \_/\_/ \___/|_|  |_|\_\

  Simple Seismological Data Processing Framework
  by HakimBMKG  |  v0.0.1(BETA)
"""


# ── Config loader ──────────────────────────────────────────────────────────────
def load_config(path: str = CFG_FILE) -> dict:
    if not os.path.exists(path):
        print(f"[ERROR] Config not found: {path}")
        print("        Run:  python seiswork.py setup  to create it.")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


# ── Info command ───────────────────────────────────────────────────────────────
def _detect_env() -> dict:
    """Detect how SeisWork runs here (conda / venv / system) and how to launch it."""
    import shutil
    py = sys.executable
    in_venv = (getattr(sys, "base_prefix", sys.prefix) != sys.prefix) and \
              not os.path.exists(os.path.join(sys.prefix, "conda-meta"))
    is_conda = bool(os.environ.get("CONDA_PREFIX")) or \
               os.path.exists(os.path.join(sys.prefix, "conda-meta"))
    has_conda = bool(shutil.which("conda"))
    venv_dir = str(Path.home() / ".venv" / "seiswork")
    start_sh = str(Path.home() / "start_seiswork.sh")
    home_bin = str(Path.home() / "bin" / "seiswork")

    if is_conda:
        mode = "conda"
        env_name = os.environ.get("CONDA_DEFAULT_ENV") or "seiswork"
        activate = f"conda activate {env_name}"
        run = "seiswork gui              # or: seiswork gui --native"
    elif in_venv or os.path.isdir(venv_dir):
        mode = "venv"
        activate = f"source {venv_dir}/bin/activate"
        run = (f"bash {start_sh}" if os.path.exists(start_sh)
               else f"{activate} && seiswork gui")
    else:
        mode = "system"
        activate = ""
        run = f"PYTHONNOUSERSITE=1 {py} seiswork.py gui"
    return {"mode": mode, "python": py, "prefix": sys.prefix,
            "has_conda": has_conda, "venv_dir": venv_dir,
            "start_sh": start_sh, "home_bin": home_bin,
            "activate": activate, "run": run}


def _service_status_line() -> str | None:
    """One-line status of the SeisWork service (systemd user unit / launchd)."""
    import platform, subprocess, shutil
    try:
        if platform.system() == "Linux" and shutil.which("systemctl"):
            r = subprocess.run(["systemctl", "--user", "is-active", _SERVICE_NAME],
                               capture_output=True, text=True, timeout=5)
            st = (r.stdout or "").strip() or "unknown"
            return f"systemd (user) '{_SERVICE_NAME}': {st}  ·  control: systemctl --user {{start|stop|restart}} {_SERVICE_NAME}"
        if platform.system() == "Darwin" and _MACOS_PLIST.exists():
            r = subprocess.run(["launchctl", "list", _MACOS_LABEL],
                               capture_output=True, text=True, timeout=5)
            st = "loaded" if r.returncode == 0 else "not loaded"
            return f"launchd '{_MACOS_LABEL}': {st}  ·  control: seiswork service {{start|stop|status}}"
    except Exception:
        pass
    return None


def cmd_info(args):
    """Print environment, tool availability, service status, and how to run."""
    import shutil
    import subprocess
    print(BANNER)

    # ── Environment & how to run (conda OR venv OR system) ─────────────────────
    e = _detect_env()
    print("  Environment")
    print("  " + "-"*56)
    print(f"  Mode         : \033[93m{e['mode']}\033[0m"
          + ("  (conda not installed → venv)" if e["mode"] == "venv" and not e["has_conda"] else ""))
    print(f"  Interpreter  : {e['python']}")
    print(f"  Env prefix   : {e['prefix']}")
    _seiswork_ok = False
    try:
        import seiswork as _sw
        _seiswork_ok = True
        print(f"  SeisWork pkg : \033[92m✓\033[0m {os.path.dirname(_sw.__file__)}")
    except Exception:
        print(f"  SeisWork pkg : \033[91m✗ not importable by this interpreter\033[0m")
    svc = _service_status_line()
    if svc:
        print(f"  Service      : {svc}")
    print()
    print("  ▶ To run the GUI")
    print("  " + "-"*56)
    if e["activate"]:
        print(f"    {e['activate']}")
    print(f"    {e['run']}")
    if e["mode"] == "venv":
        print(f"    (headless/server: seiswork gui --host 0.0.0.0 --port 5000 --no-browser)")
    print(f"    stop / restart everything:  seiswork stop   |   seiswork restart")
    print()
    # ── External tool availability ─────────────────────────────────────────────
    tools = {
        "velest"      : "1D velocity model (VELEST)",
        "hypoDD"      : "Double-difference relocation",
        "ph2dt"       : "Phase-pair preparation for HypoDD",
        "NLLoc"       : "NonLinLoc probabilistic location",
        "hypoinverse" : "Hypoinverse location",
        "seiscomp"    : "SeisComP (LocSAT backend)",
        "REAL"        : "Phase association (REAL)",
    }
    print("  External Binaries")
    print("  " + "-"*56)
    for tool, desc in tools.items():
        found = shutil.which(tool)
        if not found:
            candidate = os.path.join(os.path.expanduser("~"), "bin", tool)
            found = candidate if os.path.exists(candidate) else None
        status = f"\033[92m✓  {found}\033[0m" if found else "\033[91m✗  not found\033[0m"
        print(f"  {tool:<14} {status:<50}  {desc}")

    print()
    print("  Pipeline Steps")
    print("  " + "-"*56)
    steps = [
        ("1. pick",       "Phase picking",          "phasenet (seisbench/PyTorch GPU) | stalta | all"),
        ("2. associate",  "Phase association",       "gamma | real | all"),
        ("3. velocity",   "1D velocity model",       "velest"),
        ("4. locate",     "Hypocenter location",     "hypoinverse | locsat | nlloc | all"),
        ("5. magnitude",  "Local magnitude ML",      "(ObsPy PAZ)"),
        ("6. relocate",   "Double-diff relocation",  "catalog | crosscorr | all"),
    ]
    for step, desc, methods in steps:
        print(f"  {step:<16} {desc:<28} {methods}")

    print()
    if os.path.exists(CFG_FILE):
        cfg = load_config()
        r = cfg.get("region", {})
        print(f"  Config       : {CFG_FILE}")
        print(f"  Region       : {r.get('name','?')}")
        print(f"  Center       : {r.get('lat','?')}°N  {r.get('lon','?')}°E")
        print(f"  Period       : {r.get('starttime','?')}  →  {r.get('endtime','?')}")
    else:
        print(f"  Config  : NOT FOUND — run  seiswork setup")
    print()

    # ── Warn only when this interpreter can't import seiswork ──────────────────
    if not _seiswork_ok:
        print("\033[93m  [WARN] This interpreter cannot import seiswork.\033[0m")
        if e["activate"]:
            print(f"         Activate the env first:  {e['activate']}")
        print(f"         Then run:  {e['run']}")
        print()


# ── Setup command ──────────────────────────────────────────────────────────────
def cmd_setup(args):
    """Copy config template and create working directories."""
    from shutil import copy2
    template = BASE_DIR / "config" / "config.yaml"
    if not os.path.exists(template):
        print("[ERROR] config/config.yaml template missing from installation.")
        sys.exit(1)

    dirs = [
        "work/waveforms",
        "work/picks",
        "work/catalog",
        "work/velocity",
        "work/location/hypoinverse",
        "work/location/nlloc",
        "work/location/locsat",
        "work/magnitude",
        "work/relocation/catalog",
        "work/relocation/crosscorr",
        "work/plots",
        "work/logs",
    ]
    for d in dirs:
        os.makedirs(BASE_DIR / d, exist_ok=True)
    print("[OK] Working directories created.")
    print(f"[OK] Edit your config: {CFG_FILE}")
    print()
    print("  Quick-start:")
    print("  1. Edit config/config.yaml  (region, paths, tool options)")
    print("  2. python seiswork.py pick --method phasenet")
    print("  3. python seiswork.py associate --method gamma")
    print("  4. python seiswork.py locate --method nlloc")
    print("  5. python seiswork.py magnitude")
    print("  6. python seiswork.py relocate --method catalog")
    print("  -- or just: python seiswork.py full")


# ── Pick command ───────────────────────────────────────────────────────────────
def cmd_pick(args):
    """Run phase picker(s) on waveform data."""
    cfg = load_config()
    method = args.method.lower()

    if method in ("phasenet", "all"):
        from seiswork.modules.picker.phasenet import PhaseNetPicker
        p = PhaseNetPicker(cfg, BASE_DIR)
        p.run(workers=args.workers)

    if method in ("stalta", "all"):
        from seiswork.modules.picker.stalta import STALTAPicker
        p = STALTAPicker(cfg, BASE_DIR)
        p.run()


# ── Associate command ──────────────────────────────────────────────────────────
def cmd_associate(args):
    """Run phase association to group picks into events."""
    cfg = load_config()
    method = args.method.lower()
    picks_file = args.picks or WORK_DIR / "picks" / "picks.csv"

    if not os.path.exists(picks_file):
        print(f"[ERROR] Picks file not found: {picks_file}")
        print("        Run:  python seiswork.py pick  first.")
        sys.exit(1)

    if method in ("gamma", "all"):
        from seiswork.modules.associator.gamma import GammaAssociator
        a = GammaAssociator(cfg, BASE_DIR)
        a.run(picks_file)

    if method in ("real", "all"):
        from seiswork.modules.associator.real import RealAssociator
        a = RealAssociator(cfg, BASE_DIR)
        a.run(picks_file)


# ── Velocity command ───────────────────────────────────────────────────────────
def cmd_velocity(args):
    """Estimate 1D local velocity model with VELEST."""
    cfg = load_config()
    phases_file = args.phases or WORK_DIR / "catalog" / "phases.pha"

    if not os.path.exists(phases_file):
        print(f"[ERROR] Phase file not found: {phases_file}")
        print("        Run association step first or set --phases.")
        sys.exit(1)

    from seiswork.modules.velocity.velest import VelestVelocity
    v = VelestVelocity(cfg, BASE_DIR)
    v.run(phases_file, mode=args.mode)


# ── Locate command ─────────────────────────────────────────────────────────────
def cmd_locate(args):
    """Compute hypocenter locations."""
    cfg = load_config()
    method = args.method.lower()
    catalog_file = args.catalog or WORK_DIR / "catalog" / "catalog_associated.csv"

    if not os.path.exists(catalog_file):
        print(f"[ERROR] Catalog not found: {catalog_file}")
        print("        Run:  python seiswork.py associate  first.")
        sys.exit(1)

    if method in ("hypoinverse", "all"):
        from seiswork.modules.locator.hypoinverse import HypoinverseLocator
        loc = HypoinverseLocator(cfg, BASE_DIR)
        loc.run(catalog_file)

    if method in ("locsat", "all"):
        from seiswork.modules.locator.locsat import LocSATLocator
        loc = LocSATLocator(cfg, BASE_DIR)
        loc.run(catalog_file)

    if method in ("nlloc", "all"):
        from seiswork.modules.locator.nlloc import NLLocLocator
        loc = NLLocLocator(cfg, BASE_DIR)
        loc.run(catalog_file)


# ── Magnitude command ──────────────────────────────────────────────────────────
def cmd_magnitude(args):
    """Compute local magnitude ML for each event."""
    cfg = load_config()
    catalog_file = args.catalog or WORK_DIR / "catalog" / "catalog_located.csv"
    inventory_file = args.inventory or cfg.get("magnitude", {}).get("inventory", "")

    if not os.path.exists(catalog_file):
        print(f"[ERROR] Located catalog not found: {catalog_file}")
        print("        Run:  python seiswork.py locate  first.")
        sys.exit(1)

    from seiswork.modules.magnitude.ml import MLMagnitude
    mag = MLMagnitude(cfg, BASE_DIR)
    mag.run(catalog_file, inventory_file)


# ── Relocate command ───────────────────────────────────────────────────────────
def cmd_relocate(args):
    """Run relative relocation (HypoDD double-difference / GrowClust)."""
    cfg = load_config()
    method = args.method.lower()
    catalog_file = args.catalog or WORK_DIR / "catalog" / "catalog_ml.csv"

    if not os.path.exists(catalog_file):
        print(f"[ERROR] Catalog not found: {catalog_file}")
        sys.exit(1)

    from seiswork.modules.relocation.hypodd import HypoDDRelocation
    rel = HypoDDRelocation(cfg, BASE_DIR)

    if method in ("catalog", "all"):
        rel.run_catalog(catalog_file)

    if method in ("crosscorr", "all"):
        rel.run_crosscorr(catalog_file)

    if method in ("growclust", "all"):
        from seiswork.modules.relocation.growclust import GrowClustRelocation
        GrowClustRelocation(cfg, BASE_DIR).run_catalog(catalog_file)


# ── Detect command (template matching) ───────────────────────────────────────────
def cmd_detect(args):
    """Run template-matching detection (Match&Locate) over continuous data."""
    cfg = load_config()
    method = args.method.lower()
    # Templates come from the best available catalog (relocated → located → ML).
    catalog_file = args.catalog
    if not catalog_file:
        for cand in ("catalog_relocated.csv", "catalog_ml.csv", "catalog_nlloc.csv"):
            p = WORK_DIR / "catalog" / cand
            if os.path.exists(p):
                catalog_file = p
                break
    if not catalog_file or not os.path.exists(catalog_file):
        print(f"[ERROR] Template catalog not found: {catalog_file}")
        sys.exit(1)

    if method in ("matchlocate", "all"):
        from seiswork.modules.detection.matchlocate import MatchLocateDetection
        MatchLocateDetection(cfg, BASE_DIR).run(str(catalog_file))


# ── Plot command ───────────────────────────────────────────────────────────────
def cmd_plot(args):
    """Generate maps and figures for any pipeline step."""
    cfg = load_config()
    from seiswork.utils.plotter import SeisPlotter
    p = SeisPlotter(cfg, BASE_DIR)
    step = args.step.lower()

    if step in ("pick", "all"):
        p.plot_picks()
    if step in ("associate", "all"):
        p.plot_catalog(WORK_DIR / "catalog" / "catalog_associated.csv", tag="associated")
    if step in ("locate", "all"):
        p.plot_catalog(WORK_DIR / "catalog" / "catalog_located.csv", tag="located")
    if step in ("relocate", "all"):
        p.plot_relocation()
    if step == "all":
        p.plot_comparison()


# ── GUI command ───────────────────────────────────────────────────────────────
def _get_local_ip() -> str:
    """Return the LAN IP, preferring physical interfaces (ens/eth/wlan) over VPN/tunnel."""
    import subprocess, socket, re

    # Parse 'ip addr' and collect (iface_name, ip) pairs
    candidates: list[tuple[str, str]] = []
    try:
        out = subprocess.run(["ip", "addr"], capture_output=True, text=True, timeout=3).stdout
        iface = ""
        for line in out.splitlines():
            m_iface = re.match(r"^\d+:\s+(\S+):", line)
            if m_iface:
                iface = m_iface.group(1).rstrip(":")
            m_ip = re.match(r"\s+inet (\d+\.\d+\.\d+\.\d+)/", line)
            if m_ip and iface:
                candidates.append((iface, m_ip.group(1)))
    except Exception:
        pass

    # Priority: skip loopback + docker + tunnel/VPN; prefer ens/eth/wlan
    def _score(iface: str, ip: str) -> int:
        if ip.startswith("127."):
            return -1
        if iface in ("lo",) or iface.startswith(("docker", "br-", "virbr")):
            return -1
        # VPN / tunnel interfaces — lower priority
        if iface.startswith(("tun", "tap", "wg", "ppp", "vpn")):
            return 1
        # Physical / virtual ethernet — higher priority
        if iface.startswith(("ens", "eth", "enp", "wlan", "wlp", "em")):
            return 10
        return 5  # anything else (e.g. eno, bond, etc.)

    ranked = sorted(candidates, key=lambda x: _score(x[0], x[1]), reverse=True)
    for iface, ip in ranked:
        if _score(iface, ip) >= 1:
            return ip

    # Absolute fallback
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


# Keep the NSImage + timer target alive; if GC'd, the Dock icon goes blank.
_DOCK_ICON_REFS: list = []


def _brand_native_app(name: str, icon_path: str | None = None) -> None:
    """macOS only: set the app name and Dock icon (a bare python process shows
    as 'Python' with the rocket icon). No-op elsewhere.

    For the icon to stick: (1) activation policy must be Regular, and
    (2) the icon must be re-applied after pywebview starts, because pywebview
    resets it during launch (done via NSTimers on the main run loop)."""
    # Override the bundle name (menu bar / cmd-Tab / Dock).
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        if bundle is not None:
            info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
            if info is not None:
                info["CFBundleName"] = name
                info["CFBundleDisplayName"] = name
    except Exception:
        pass

    try:
        from AppKit import NSApplication, NSImage
        app = NSApplication.sharedApplication()
        # Regular app -> gets a real Dock tile whose icon we can set.
        try:
            app.setActivationPolicy_(0)   # NSApplicationActivationPolicyRegular
        except Exception:
            pass
        image = None
        if icon_path and os.path.exists(icon_path):
            # initWithContentsOfFile_ decodes the PNG now (the lazy variant can read empty).
            image = NSImage.alloc().initWithContentsOfFile_(icon_path)
        if image is not None and image.isValid():
            _DOCK_ICON_REFS.append(image)
            app.setApplicationIconImage_(image)
            _schedule_dock_icon_reapply(app, image)
    except Exception:
        pass


def _schedule_dock_icon_reapply(app, image) -> None:
    """Re-apply the Dock icon a few times after startup (pywebview overwrites it)."""
    try:
        from Foundation import NSObject, NSTimer

        class _SWIconSetter(NSObject):
            def reapply_(self, _timer):
                try:
                    app.setApplicationIconImage_(image)
                except Exception:
                    pass

        setter = _SWIconSetter.alloc().init()
        _DOCK_ICON_REFS.append(setter)   # keep alive (GC would drop the timer target)
        for delay in (0.2, 0.6, 1.2, 2.5, 4.0):
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                delay, setter, b"reapply:", None, False)
    except Exception:
        pass


def _native_splash_html(duration_secs: float = 10.0) -> str:
    """Splash page for the pywebview window, shown while the server boots.
    The image is embedded as base64 so it paints before the server is up;
    the progress bar fills over `duration_secs`."""
    import base64
    img_tag = ""
    png = Path(__file__).resolve().parent / "web" / "static" / "img" / "og-preview.png"
    try:
        b64 = base64.b64encode(png.read_bytes()).decode("ascii")
        img_tag = f'<img src="data:image/png;base64,{b64}" alt="SeisWork"/>'
    except Exception:
        pass  # no image -> splash still shows title + progress bar
    dur_ms = int(max(1.0, duration_secs) * 1000)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
      :root {{
        --bg-panel: rgba(13,19,32,.94);
        --bg-card: rgba(13,19,32,.94);
        --border: rgba(255,255,255,.06);
        --text-main: #cbd5e1;
        --text-muted: #8aa0bd;
        --track-bg: #1a2436;
      }}
      @media (prefers-color-scheme: light) {{
        :root {{
          --bg-panel: rgba(255,255,255,.94);
          --bg-card: rgba(255,255,255,.94);
          --border: rgba(0,0,0,.1);
          --text-main: #1e293b;
          --text-muted: #64748b;
          --track-bg: #e2e8f0;
        }}
      }}
      *{{box-sizing:border-box}}
      html,body{{margin:0;height:100%;background:transparent;overflow:hidden}}
      .wrap{{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
             font-family:system-ui,-apple-system,'Segoe UI',sans-serif}}
      .card{{display:flex;flex-direction:column;align-items:center;gap:1rem;padding:24px;
             max-width:94vw;max-height:94vh;border-radius:20px;
             background:var(--bg-card);border:1px solid var(--border);
             box-shadow:0 20px 60px rgba(0,0,0,.55)}}
      img{{width:100%;max-width:100%;max-height:50vh;object-fit:contain;border-radius:12px}}
      .title{{font-size:1.3rem;font-weight:700;letter-spacing:.5px;color:var(--text-main)}}
      .title span{{color:#3b82f6}}
      .loader{{width:320px;max-width:82vw;display:flex;flex-direction:column;gap:.4rem}}
      .row{{display:flex;justify-content:space-between;font-size:.72rem}}
      .msg{{color:var(--text-muted);letter-spacing:.2px}}
      .pct{{color:#3b82f6;font-weight:700;font-variant-numeric:tabular-nums}}
      .track{{height:8px;background:var(--track-bg);border-radius:6px;overflow:hidden}}
      .bar{{height:100%;width:0%;border-radius:6px;
            background:linear-gradient(90deg,#2563eb,#60a5fa);transition:width .18s ease}}
    </style></head><body><div class="wrap"><div class="card">
      {img_tag}
      <div class="title">Seis<span>Work</span></div>
      <div class="loader">
        <div class="row"><span class="msg" id="sw-msg">Starting server…</span>
                         <span class="pct" id="sw-pct">0%</span></div>
        <div class="track"><div class="bar" id="sw-bar"></div></div>
      </div>
    </div></div>
    <script>
      var DUR = {dur_ms}, t0 = Date.now();
      var phases = [[0,'Starting server…'],[25,'Loading interface…'],
                    [55,'Preparing map & tiles…'],[85,'Almost ready…']];
      var bar = document.getElementById('sw-bar'),
          pctEl = document.getElementById('sw-pct'),
          msgEl = document.getElementById('sw-msg');
      function tick(){{
        var pct = Math.min(99, Math.round((Date.now()-t0)/DUR*100));
        bar.style.width = pct+'%'; pctEl.textContent = pct+'%';
        for (var i=phases.length-1;i>=0;i--){{ if(pct>=phases[i][0]){{ msgEl.textContent=phases[i][1]; break; }} }}
        if (pct<99) requestAnimationFrame(tick);
      }}
      requestAnimationFrame(tick);
    </script></body></html>"""


def _ensure_linux_gi_typelib_path():
    """Linux only: conda PyGObject does not search the system girepository dir,
    so Gtk typelibs are "not available". Point GI_TYPELIB_PATH at them."""
    import platform, glob
    if platform.system() != "Linux":
        return
    candidates = [
        "/usr/lib/x86_64-linux-gnu/girepository-1.0",
        "/usr/lib/aarch64-linux-gnu/girepository-1.0",
        "/usr/lib64/girepository-1.0",
        "/usr/lib/girepository-1.0",
    ]
    found = [p for p in candidates if glob.glob(os.path.join(p, "Gtk-3.0.typelib"))]
    if not found:
        return
    existing = os.environ.get("GI_TYPELIB_PATH", "")
    for p in found:
        if p not in existing.split(os.pathsep):
            existing = f"{p}{os.pathsep}{existing}" if existing else p
    os.environ["GI_TYPELIB_PATH"] = existing


def _harden_webkit_native_window():
    """Linux only: WebKitGTK's GPU (DMA-BUF) compositing crashes in VMs /
    containers / remote desktops, so the native window closes instantly while
    the server keeps running. Disable DMA-BUF always (cheap, big reliability
    win); force full software rendering in a VM or without a usable render node."""
    import platform, glob
    if platform.system() != "Linux":
        return
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
    dri_ok = any(
        os.access(p, os.R_OK | os.W_OK)
        for p in glob.glob("/dev/dri/render*") + glob.glob("/dev/dri/card*")
    )
    # A readable render node can still be a virtual GPU that crashes WebKit,
    # so in a VM force full software rendering.
    in_vm = False
    try:
        import subprocess
        r = subprocess.run(["systemd-detect-virt"], capture_output=True, text=True, timeout=3)
        in_vm = r.returncode == 0 and (r.stdout or "").strip() not in ("", "none")
    except Exception:
        try:
            vendor = open("/sys/class/dmi/id/sys_vendor").read().lower()
            in_vm = any(v in vendor for v in ("qemu", "vmware", "virtualbox", "innotek", "microsoft", "xen", "parallels"))
        except Exception:
            in_vm = False
    if in_vm or not dri_ok:
        os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
        os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
        os.environ.setdefault("GDK_GL", "disable")


def cmd_gui(args):
    """Launch the SeisWork web GUI in a browser."""
    import webbrowser
    import threading
    from seiswork.web.app import run as gui_run

    host       = args.host
    # port None = not pinned -> auto-detect; an explicit --port pins it.
    port       = args.port if getattr(args, "port", None) is not None else 5000
    debug      = args.debug
    no_browser = getattr(args, "no_browser", False)
    force      = getattr(args, "force", False)

    # ── Guard: never run two servers by accident ──────────────────────────────
    # A stale instance keeps serving old code. Take over (--force) or refuse.
    _existing = _seiswork_server_procs()
    if _existing and force:
        import time as _t
        print(f"  --force: stopping {len(_existing)} running SeisWork component(s) first…")
        _stop_procs([e["pid"] for e in _existing])
        _clear_gui_state()
        _t.sleep(0.6)
        _existing = []
    # The Online Viewer mirror is NOT a competing instance: it survives restarts
    # by design and the new server reuses it. Counting it here caused an endless
    # systemd crash-loop on every restart.
    _existing = [e for e in _existing if e.get("kind") != "viewer"]
    if _existing:
        # A running GUI blocks a second launch. Show a native popup, not just
        # console text — the user may never see the terminal.
        running = _detect_running_gui()
        if running and running.get("pid") == os.getpid():
            running = None   # our own re-exec, not a competing instance
        if running and not _gui_health_ok(running["port"]):
            # Holds the port but fails /api/health -> wedged half-start.
            # Reap it and take over instead of failing forever.
            print(f"  ⚠ SeisWork pid {running['pid']} holds port {running['port']} "
                  f"but does not answer /api/health — stale/wedged instance, replacing it.")
            _stop_procs([running["pid"]])
            _clear_gui_state()
            running = None
        if running:
            rport = running["port"]
            msg = (f"SeisWork already open on port {rport}.\n\n"
                   f"Please check the runner, or just open its window with:\n"
                   f"    seiswork open\n\n"
                   f"To force a clean restart:  seiswork restart")
            _gui_alert("SeisWork already open", msg)
            print(f"  ✗ SeisWork already open on port {rport} (pid {running['pid']}).")
            print(f"    • Open its window only:  seiswork open")
            print(f"    • Restart fresh:         seiswork restart")
            print(f"    • Take over this port:   seiswork gui --force --port {rport}")
            sys.exit(1)

    # A busy port would crash the server. On macOS, AirPlay holds ports
    # 5000/7000 (replies 403). Detect a busy port and move to a free one.
    import socket, platform, time
    _bind_host = "0.0.0.0" if host in ("0.0.0.0", "") else host
    def _port_free(p: int) -> bool:
        # Bind with the same host and SO_REUSEADDR flag Werkzeug uses.
        # REUSEADDR ignores TIME_WAIT leftovers from a just-killed server
        # (a plain bind reports those as falsely "busy"), while a live
        # listener (e.g. AirPlay) still fails -> real conflicts detected.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((_bind_host, p))
                return True
            except OSError:
                return False
    if not _port_free(port):
        # A restart can briefly leave the old instance on the port. Wait for it
        # to release so a restart always comes back on the SAME port.
        for _ in range(15):
            time.sleep(0.4)
            if _port_free(port):
                break
    if not _port_free(port):
        # Still busy: if no port was pinned, pick the next free one and say so;
        # if the port WAS pinned, fail clearly instead of silently drifting.
        pinned = getattr(args, "port", None) is not None
        if not pinned:
            busy = port
            candidates = [port + 1, 8080, 8000, 8501, 5050, 5555] + list(range(5001, 5030))
            port = next((c for c in candidates if _port_free(c)), None)
            if port is None:
                print(f"  ✗ Port {busy} busy and no free fallback port found.")
                sys.exit(1)
            _air = platform.system() == "Darwin" and busy in (5000, 7000)
            why = " (held by macOS AirPlay Receiver)" if _air else " (in use)"
            print(f"  ⚠ Port {busy}{why} → auto-detected free port {port}.")
        elif platform.system() == "Darwin" and port in (5000, 7000):
            busy = port
            for cand in (port + 1, 8080, 8000, 8501):
                if _port_free(cand):
                    port = cand
                    break
            print(f"  ⚠ Port {busy} is held by macOS AirPlay Receiver → using {port}. "
                  f"(Disable it under System Settings › General › AirDrop & Handoff to keep {busy}.)")
        else:
            print(f"  ✗ Port {port} is still in use. Free it, or start with --port <N>.")
            sys.exit(1)

    # Resolve display URL — when binding 0.0.0.0 show the real IP
    if host in ("0.0.0.0", ""):
        display_ip = _get_local_ip()
    else:
        display_ip = host
    browser_url = f"http://{display_ip}:{port}"

    print(BANNER)
    print(f"  SeisWork GUI  →  {browser_url}")
    if host == "0.0.0.0":
        print(f"  (bind 0.0.0.0 — accessible from the local network)")
    print(f"  Press Ctrl+C to stop the server.\n")

    # Record the chosen host/port so a second launch can detect this instance and
    # `seiswork open` can attach to it (removed on exit via atexit).
    _write_gui_state(os.getpid(), host, port, browser_url)

    native = getattr(args, "native", False)
    if native:
        # Native desktop window (WKWebView/WebKitGTK). Starts Flask on a bg thread
        # then owns the main thread for the UI loop. Falls back to the browser when
        # pywebview is unavailable.
        if _run_native_window(host, port, start_server=True):
            return
        # pywebview missing → browser fallback below.

    if not debug and not no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(browser_url)).start()

    gui_run(host=host, port=port, debug=debug)


# ── Service management (systemd user service) ─────────────────────────────────
_SERVICE_NAME = "seiswork"
_SERVICE_DIR  = Path.home() / ".config" / "systemd" / "user"
_SERVICE_FILE = _SERVICE_DIR / f"{_SERVICE_NAME}.service"


def _service_unit(port: int = 5000) -> str:
    """Generate systemd unit content for the current installation."""
    import shutil
    python  = shutil.which("python") or sys.executable
    sw_bin  = shutil.which("seiswork") or str(Path(python).parent / "seiswork")
    core_bin = str(BASE_DIR / "core" / "bin")
    home_bin = str(Path.home() / "bin")
    env_path = f"{Path(python).parent}:{core_bin}:{home_bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    return (
        "[Unit]\n"
        "Description=SeisWork — Seismological Processing Web GUI\n"
        "Documentation=https://github.com/HakimBMKG/seiswork\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={BASE_DIR}\n"
        f"ExecStart={sw_bin} gui --host 0.0.0.0 --port {port} --no-browser\n"
        "Environment=PYTHONNOUSERSITE=1\n"
        f"Environment=PATH={env_path}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _ensure_xdg_runtime() -> bool:
    """Set XDG_RUNTIME_DIR when missing (SSH/headless). Return True when ready."""
    import subprocess
    if os.environ.get("XDG_RUNTIME_DIR"):
        return True
    uid = os.getuid()
    candidate = f"/run/user/{uid}"
    if os.path.isdir(candidate):
        os.environ["XDG_RUNTIME_DIR"] = candidate
        return True
    # Try triggering linger so systemd-logind creates /run/user/UID.
    # loginctl may be absent (minimal container / non-systemd) — don't crash.
    try:
        subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")],
                       capture_output=True)
    except FileNotFoundError:
        return False
    if os.path.isdir(candidate):
        os.environ["XDG_RUNTIME_DIR"] = candidate
        return True
    return False


def _systemctl(args_list: list, capture: bool = False):
    import subprocess
    env = os.environ.copy()
    cmd = ["systemctl", "--user"] + args_list
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, env=env)
    return subprocess.run(cmd, env=env)


# ── Service access URLs (shown on 'status'/'install' so the user knows where the
#    GUI is) — works on macOS and Linux via _get_local_ip()'s socket fallback. ──
def _service_urls(port) -> list:
    ip = _get_local_ip()
    urls = [f"http://localhost:{port}"]
    if ip and ip != "127.0.0.1":
        urls.append(f"http://{ip}:{port}")
    return urls


# ── macOS launchd LaunchAgent (the counterpart of the Linux systemd service) ──
_MACOS_LABEL = "com.seiswork.gui"
_MACOS_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.seiswork.gui.plist"
_MACOS_LOG   = Path.home() / "Library" / "Logs"


def _macos_service_port(default: int = 5000) -> int:
    """Read the --port the LaunchAgent was installed with (from its .plist)."""
    import plistlib
    try:
        with open(_MACOS_PLIST, "rb") as f:
            pa = plistlib.load(f).get("ProgramArguments", [])
        for i, a in enumerate(pa):
            if a == "--port" and i + 1 < len(pa):
                return int(pa[i + 1])
    except Exception:
        pass
    return default


def _macos_agent_state():
    """(installed, pid): pid>0 running, None loaded-not-running, -1 not loaded."""
    import subprocess, re
    if not _MACOS_PLIST.exists():
        return (False, -1)
    r = subprocess.run(["launchctl", "list", _MACOS_LABEL],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return (True, -1)
    m = re.search(r'"PID"\s*=\s*(\d+)', r.stdout)
    return (True, int(m.group(1))) if m else (True, None)


def _service_macos(action, args):
    """Manage / report the launchd LaunchAgent on macOS."""
    import subprocess
    plist_disp = "~/Library/LaunchAgents/com.seiswork.gui.plist"

    def _reload():
        subprocess.run(["launchctl", "unload", str(_MACOS_PLIST)], capture_output=True)
        return subprocess.run(["launchctl", "load", "-w", str(_MACOS_PLIST)],
                              capture_output=True)

    if action == "status":
        installed, pid = _macos_agent_state()
        if not installed:
            print("[service] launchd agent not installed.")
            print("          Install it:  bash install.sh --step 8")
            return
        port = _macos_service_port()
        if isinstance(pid, int) and pid > 0:
            state = f"running (pid {pid})"
        elif pid is None:
            state = "loaded but not running — check the log"
        else:
            state = "installed but not loaded"
        print(f"[service] launchd agent : {_MACOS_LABEL}")
        print(f"[service] state         : {state}")
        print(f"[service] port          : {port}")
        for u in _service_urls(port):
            print(f"  SeisWork GUI  →  {u}")
        print(f"[service] plist         : {plist_disp}")
        print(f"[service] logs          : ~/Library/Logs/seiswork.gui.out.log (and .err.log)")
        return

    if action in ("start", "enable"):
        if not _MACOS_PLIST.exists():
            print("[service] not installed — run: bash install.sh --step 8"); return
        _reload()
        port = _macos_service_port()
        print("[service] loaded — GUI auto-starts now and at login")
        for u in _service_urls(port):
            print(f"  SeisWork GUI  →  {u}")
        return

    if action in ("stop", "disable"):
        subprocess.run(["launchctl", "unload", str(_MACOS_PLIST)], capture_output=True)
        print("[service] unloaded — GUI stopped and disabled")
        return

    if action == "restart":
        if not _MACOS_PLIST.exists():
            print("[service] not installed — run: bash install.sh --step 8"); return
        _reload()
        port = _macos_service_port()
        print("[service] restarted")
        for u in _service_urls(port):
            print(f"  SeisWork GUI  →  {u}")
        return

    if action == "install":
        print("[service] On macOS the LaunchAgent is created by the installer:")
        print("            bash install.sh --step 8")
        print("          (resolves the env python, picks a free port avoiding AirPlay,")
        print("           writes the .plist and loads it).")
        return

    if action == "remove":
        subprocess.run(["launchctl", "unload", str(_MACOS_PLIST)], capture_output=True)
        if _MACOS_PLIST.exists():
            _MACOS_PLIST.unlink()
            print(f"[service] removed {plist_disp}")
        else:
            print("[service] nothing to remove (no plist).")
        return

    if action == "log":
        n = getattr(args, "lines", 50)
        shown = False
        for lf in (_MACOS_LOG / "seiswork.gui.err.log",
                   _MACOS_LOG / "seiswork.gui.out.log"):
            if lf.exists():
                shown = True
                print(f"── {lf} (last {n}) ──", flush=True)
                subprocess.run(["tail", "-n", str(n), str(lf)])
        if not shown:
            print("[service] no log files yet at ~/Library/Logs/seiswork.gui.*.log")
        return


def _read_unit_port(default: int = 5000) -> int:
    """Read the --port the systemd unit was installed with (from its unit file)."""
    import re
    try:
        m = re.search(r"--port\s+(\d+)", _SERVICE_FILE.read_text())
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return default


# ── Process discovery: running SeisWork server components ──────────────────────
# Match only python processes running a known entrypoint (never a bare
# "seiswork" substring, so shells/editors with the repo path in args don't
# match). The no-".py" variants match the installed console script
# (e.g. the systemd unit's ExecStart).
_SERVER_ENTRYPOINTS = ("seiswork gui", "seiswork restart",
                       "seiswork.py gui", "seiswork.py restart",
                       "seiswork.web.app", "seiswork/web/app.py", "seiswork-agent")


def _seiswork_server_procs(extra_exclude=None):
    """List running SeisWork server/agent processes as [{'pid','cmd','port','kind'}].
    Excludes this process and its ancestors so `restart` never kills itself.
    Returns [] if psutil is missing."""
    import re
    try:
        import psutil
    except ImportError:
        return []
    exclude = set(extra_exclude or ())
    try:
        p = psutil.Process(os.getpid())
        while p is not None:
            exclude.add(p.pid)
            p = p.parent()
    except Exception:
        exclude.add(os.getpid())

    found = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if proc.pid in exclude:
            continue
        try:
            argv = proc.info.get("cmdline") or []
            name = (proc.info.get("name") or "")
        except Exception:
            continue
        cmd = " ".join(argv)
        # Must be a python interpreter running a known entrypoint.
        is_python = ("python" in name.lower()
                     or (argv and "python" in os.path.basename(argv[0]).lower()))
        # (entrypoint list is gui/web/agent only)
        if not is_python or not any(e in cmd for e in _SERVER_ENTRYPOINTS):
            continue
        if "seiswork-agent" in cmd:
            kind = "agent"
        elif "web.app" in cmd or "web/app.py" in cmd:
            kind = "web"
        elif "gui" in cmd or "restart" in cmd:
            # `restart` runs the GUI server in-process — classify it as gui.
            kind = "gui"
        else:
            kind = "server"
        m = re.search(r"--port[= ](\d+)", cmd)
        port = int(m.group(1)) if m else None
        # The read-only Online Viewer mirror (port 3346) survives restarts by
        # design. Classify it separately so it never trips the single-instance
        # guard (that caused an endless systemd crash-loop); stop/restart/--force
        # still reap it.
        if kind == "web":
            try:
                if proc.environ().get("SEISWORK_VIEWER_MODE") == "1":
                    kind = "viewer"
            except Exception:
                # environ() can be denied (macOS) — fall back to the spawn signature.
                viewer_port = int(os.environ.get("SEISWORK_VIEWER_PORT", "3346"))
                if "seiswork.web.app" in cmd and port == viewer_port:
                    kind = "viewer"
        found.append({"pid": proc.pid, "cmd": cmd, "kind": kind, "port": port})
    return found


def _stop_procs(pids, label="component"):
    """SIGTERM the given pids, wait up to 5s, then SIGKILL survivors."""
    try:
        import psutil
    except ImportError:
        return 0
    objs = []
    for pid in pids:
        try:
            p = psutil.Process(pid); p.terminate(); objs.append(p)
        except Exception:
            pass
    gone, alive = psutil.wait_procs(objs, timeout=5)
    for p in alive:
        try:
            p.kill()
            print(f"    ↳ force-killed pid {p.pid}")
        except Exception:
            pass
    return len(objs)


# ── Service-aware stop/restart ────────────────────────────────────────────────
# Under launchd/systemd, killing PIDs alone doesn't stop SeisWork — the manager
# respawns it. Stop/restart must go through the service manager first, then
# reap stray processes and free the ports.
def _service_installed() -> str | None:
    """Return 'launchd' / 'systemd' when a SeisWork service is installed, else None."""
    import platform, shutil, subprocess
    if platform.system() == "Darwin" and _MACOS_PLIST.exists():
        return "launchd"
    if platform.system() == "Linux" and shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "--user", "cat", _SERVICE_NAME],
                           capture_output=True)
        if r.returncode == 0:
            return "systemd"
    return None


def _stop_seiswork_service() -> str | None:
    """Stop the service so KeepAlive/auto-restart does NOT respawn killed procs."""
    import platform, shutil, subprocess
    kind = _service_installed()
    if kind == "launchd":
        uid = os.getuid()
        # bootout (modern) + unload (fallback) — either stops the respawning.
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{_MACOS_LABEL}"],
                       capture_output=True)
        subprocess.run(["launchctl", "unload", str(_MACOS_PLIST)], capture_output=True)
        return f"launchd {_MACOS_LABEL}"
    if kind == "systemd":
        subprocess.run(["systemctl", "--user", "stop", _SERVICE_NAME], capture_output=True)
        return f"systemd {_SERVICE_NAME}"
    return None


def _restart_seiswork_service() -> str | None:
    """Cleanly restart the service (kill + relaunch fresh code), keeping it managed."""
    import platform, shutil, subprocess
    kind = _service_installed()
    if kind == "launchd":
        uid = os.getuid()
        # Ensure loaded, then kickstart -k = atomic kill + restart.
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(_MACOS_PLIST)],
                       capture_output=True)
        subprocess.run(["launchctl", "enable", f"gui/{uid}/{_MACOS_LABEL}"],
                       capture_output=True)
        r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{_MACOS_LABEL}"],
                          capture_output=True, text=True)
        return f"launchd {_MACOS_LABEL}" if r.returncode == 0 else None
    if kind == "systemd":
        r = subprocess.run(["systemctl", "--user", "restart", _SERVICE_NAME],
                          capture_output=True)
        return f"systemd {_SERVICE_NAME}" if r.returncode == 0 else None
    return None


# Pipeline job runners + slarchive that a running SeisWork spawns as children.
_PROC_EXTRA_MATCH = ("_pick_runner.py", "_pipeline_runner.py",
                     "_realtime_pipeline", "_denoise_runner.py")


def _seiswork_related_procs():
    """All SeisWork processes: servers plus pipeline runners and slarchive.
    Self + ancestors excluded."""
    procs = _seiswork_server_procs()
    seen = {p["pid"] for p in procs}
    try:
        import psutil
    except ImportError:
        return procs
    exclude = set()
    try:
        p = psutil.Process(os.getpid())
        while p is not None:
            exclude.add(p.pid); p = p.parent()
    except Exception:
        exclude.add(os.getpid())
    repo = str(Path(__file__).resolve().parents[1])   # …/seiswork repo root
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if proc.pid in exclude or proc.pid in seen:
            continue
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
        except Exception:
            continue
        if not cmd:
            continue
        kind = None
        if any(e in cmd for e in _PROC_EXTRA_MATCH):
            kind = "runner"
        elif "slarchive" in cmd and ("online_sds" in cmd or "/seiswork/work" in cmd
                                     or repo in cmd):
            kind = "slarchive"
        if kind:
            procs.append({"pid": proc.pid, "cmd": cmd, "kind": kind, "port": None})
            seen.add(proc.pid)
    return procs


def _stop_all_seiswork(stop_service: bool = True, quiet: bool = False) -> int:
    """Stop the service (so nothing respawns), then reap every SeisWork process
    and clear the portfile. Returns the count killed."""
    import time
    if stop_service:
        svc = _stop_seiswork_service()
        if svc and not quiet:
            print(f"  ✓ Stopped service: {svc} (no auto-respawn)")
    total = 0
    for _ in range(4):
        procs = _seiswork_related_procs()
        if not procs:
            break
        if not quiet:
            for info in procs:
                port = f" :{info['port']}" if info.get("port") else ""
                short = info["cmd"] if len(info["cmd"]) <= 80 else info["cmd"][:77] + "…"
                print(f"    • [{info['kind']:8}] pid {info['pid']}{port}  {short}")
        _stop_procs([p["pid"] for p in procs])
        total += len(procs)
        time.sleep(0.4)
    _clear_gui_state()
    return total


# ── Running-GUI discovery: portfile (source of truth) + psutil fallback ─────────
# The GUI writes ~/.seiswork_gui.json on start so a second launch knows the
# exact host/port. Drives the single-instance popup and `seiswork open`.
_GUI_STATE_FILE = Path.home() / ".seiswork_gui.json"


def _write_gui_state(pid: int, host: str, port: int, url: str) -> None:
    import json, atexit
    try:
        _GUI_STATE_FILE.write_text(json.dumps(
            {"pid": pid, "host": host, "port": port, "url": url,
             "started": __import__("datetime").datetime.now().isoformat(timespec="seconds")}))
        atexit.register(_clear_gui_state, pid)
    except Exception:
        pass


def _clear_gui_state(only_pid: int | None = None) -> None:
    import json
    try:
        if only_pid is not None and _GUI_STATE_FILE.exists():
            st = json.loads(_GUI_STATE_FILE.read_text())
            if st.get("pid") != only_pid:
                return   # someone else owns it now
        _GUI_STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _proc_listen_port(pid: int) -> int | None:
    """Actual LISTEN port of a process (reliable on Linux; may be denied on macOS)."""
    try:
        import psutil
        p = psutil.Process(pid)
        conns = p.net_connections(kind="inet") if hasattr(p, "net_connections") else p.connections(kind="inet")
        for c in conns:
            if c.status == psutil.CONN_LISTEN and c.laddr:
                return int(c.laddr.port)
    except Exception:
        pass
    return None


def _detect_running_gui() -> dict | None:
    """Return {'pid','host','port','url'} of a live SeisWork GUI/web server, or None.
    Prefers the portfile, falls back to a psutil scan."""
    import json
    # 1) Portfile — verify the pid is still a live SeisWork process.
    try:
        if _GUI_STATE_FILE.exists():
            st = json.loads(_GUI_STATE_FILE.read_text())
            pid = int(st.get("pid") or 0)
            # After a hot-reload re-exec (same pid) the portfile points at THIS
            # process — our own restart, not a competing instance. Reporting it
            # made the guard block, or even SIGTERM itself.
            _self = {os.getpid()}
            try:
                import psutil as _ps
                p = _ps.Process(os.getpid())
                while p is not None:
                    _self.add(p.pid)
                    p = p.parent()
            except Exception:
                pass
            if pid in _self:
                return None
            try:
                import psutil
                # Broad "seiswork" match so a server launched via `seiswork
                # restart` is still recognized.
                _cl = " ".join(psutil.Process(pid).cmdline()) if psutil.pid_exists(pid) else ""
                alive = "seiswork" in _cl.lower()
            except Exception:
                alive = False
            if alive:
                port = int(st.get("port") or 0) or 5000
                host = st.get("host") or "127.0.0.1"
                return {"pid": pid, "host": host, "port": port,
                        "url": st.get("url") or f"http://127.0.0.1:{port}"}
            else:
                _clear_gui_state()   # stale portfile
    except Exception:
        pass
    # 2) psutil scan (no portfile). "viewer" (the port-3346 mirror) is excluded —
    # it is a companion, not the GUI; counting it broke the single-instance popup.
    web = [e for e in _seiswork_server_procs() if e["kind"] in ("gui", "web")]
    if not web:
        return None
    e = web[0]
    port = e["port"] or _proc_listen_port(e["pid"]) or 5000
    return {"pid": e["pid"], "host": "127.0.0.1", "port": port,
            "url": f"http://127.0.0.1:{port}"}


def _gui_health_ok(port: int, timeout: float = 4.0) -> bool:
    """True when the server actually answers HTTP. A pid can hold the LISTEN
    socket yet never accept (wedged half-start); only an HTTP round-trip tells."""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def _gui_alert(title: str, message: str) -> bool:
    """Best-effort native popup (osascript / zenity / kdialog / tkinter).
    Returns True if shown. SEISWORK_NO_GUI_ALERT=1 suppresses it."""
    import platform, shutil, subprocess
    if os.environ.get("SEISWORK_NO_GUI_ALERT") == "1":
        return False
    # Under a service manager nobody can click the dialog — a blocking popup
    # just wedges the restart (and caused a slow systemd crash-loop). Skip it.
    if os.environ.get("INVOCATION_ID") or os.environ.get("XPC_SERVICE_NAME"):
        return False
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            script = (f'display dialog "{message}" with title "{title}" '
                      f'buttons {{"OK"}} default button "OK" with icon caution')
            subprocess.run(["osascript", "-e", script], timeout=60,
                           capture_output=True)
            return True
        if sysname == "Linux":
            if shutil.which("zenity"):
                subprocess.run(["zenity", "--warning", "--title", title,
                                "--text", message], timeout=60)
                return True
            if shutil.which("kdialog"):
                subprocess.run(["kdialog", "--title", title, "--sorry", message],
                               timeout=60)
                return True
        # No zenity/kdialog -> tkinter (usually bundled).
        import tkinter as _tk
        from tkinter import messagebox as _mb
        _root = _tk.Tk(); _root.withdraw()
        _mb.showwarning(title, message)
        _root.destroy()
        return True
    except Exception:
        return False


def _run_native_window(host: str, port: int, start_server: bool = True) -> bool:
    """Open the GUI in a native desktop window (pywebview). start_server=False
    attaches to an already-running server (`seiswork open`). Returns False when
    pywebview is unavailable so the caller falls back to the browser."""
    # Headless machine (no DISPLAY/Wayland) can't show a native window.
    import platform as _plat
    if _plat.system() == "Linux" and not (os.environ.get("DISPLAY")
                                          or os.environ.get("WAYLAND_DISPLAY")):
        print("  ⚠ No graphical display (headless) — cannot open a native window.")
        return False

    _ensure_linux_gi_typelib_path()
    _harden_webkit_native_window()
    try:
        import webview  # pywebview
    except ImportError:
        print("  ⚠ Native mode needs pywebview — falling back to the browser.")
        print("    Install it with:  pip install pywebview")
        return False

    import threading, socket, time
    local_url = f"http://127.0.0.1:{port}"
    # Under some editable installs `seiswork.__version__` is missing
    # (namespace package) — read it defensively.
    try:
        from seiswork import __version__
    except Exception:
        __version__ = "0.0.1(BETA)"
    win_title = f"SeisWork {__version__}"
    _icon_png = os.path.join(os.path.dirname(__file__),
                             "web", "static", "img", "seiswork-icon.png")
    _brand_native_app(win_title, _icon_png)

    _PAGE_TITLES = {
        "waveform-fullpage":    "Waveform & Spectrogram",
        "station-map-fullpage": "Station Map",
        "catalog-map":          "Event Catalog",
        "pipeline-flow":        "Pipeline Flow",
        "present":              "Presentation",
    }

    def _title_for(url):
        for key, label in _PAGE_TITLES.items():
            if key in url:
                return f"SeisWork — {label}"
        return win_title

    class _NativeApi:
        def open_window(self, url):
            try:
                if not url:
                    return False
                if url.startswith("/"):
                    url = local_url + url
                webview.create_window(_title_for(url), url=url,
                                      width=1280, height=860, js_api=_NativeApi())
            except Exception:
                pass
            return True

        def close_window(self):
            try:
                win = webview.active_window()
                if win is not None:
                    win.destroy()
            except Exception:
                pass
            return True
    native_api = _NativeApi()

    if start_server:
        from seiswork.web.app import run as gui_run
        threading.Thread(
            target=lambda: gui_run(host=host, port=port, debug=False),
            daemon=True,
        ).start()

    print("  Opening native window (pywebview)…")

    # ── Transparent frameless splash → hidden main window → reveal ───────────
    # Safe on Linux and macOS: past WebProcess crashes were the GPU renderer
    # (handled above), never the splash transparency.
    SPLASH_MIN_SECS = 10.0
    splash = webview.create_window(
        win_title, html=_native_splash_html(SPLASH_MIN_SECS),
        width=680, height=520, frameless=True, on_top=True, resizable=False,
        transparent=True)
    main = webview.create_window(
        win_title, html=_native_splash_html(),
        width=1440, height=900, hidden=True, js_api=native_api)

    state = {"shown": False, "booted": False}
    app_loaded = threading.Event()
    start_ts = time.monotonic()

    def _reveal():
        if state["shown"]:
            return
        state["shown"] = True
        # pywebview never clears Window.hidden, and its GTK backend re-hides the
        # window on every show() while that flag is true — so a hidden=True
        # window stays unmapped forever unless we clear the flag here first.
        main.hidden = False
        try:    main.show()
        except Exception: pass
        try:    splash.destroy()
        except Exception: pass

    def _on_main_loaded():
        if state["booted"]:
            app_loaded.set()

    main.events.loaded += _on_main_loaded

    def _boot():
        for _ in range(150):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    break
            except OSError:
                time.sleep(0.2)
        state["booted"] = True
        main.load_url(local_url)
        app_loaded.wait(timeout=20)
        remaining = SPLASH_MIN_SECS - (time.monotonic() - start_ts)
        if remaining > 0:
            time.sleep(remaining)
        _reveal()

    # No native OS menu bar — Help/Changelog live in the in-page navbar.
    native_menu = []

    # icon: sets the Linux window/taskbar icon (harmless on macOS).
    # private_mode=False: the default private session makes localStorage null in
    # WebKitGTK, which broke the app JS -> blank "Loading interface..." page.
    try:
        webview.start(_boot, icon=(_icon_png if os.path.exists(_icon_png) else None),
                      private_mode=False, menu=native_menu)
    except TypeError:
        webview.start(_boot, private_mode=False)   # older pywebview lacks the kwargs
    return True


def cmd_stop(args):
    """Stop everything: service, servers, runners, slarchive — free all ports."""
    try:
        import psutil  # noqa: F401
    except ImportError:
        print("  ✗ 'stop' needs psutil:  pip install psutil")
        sys.exit(1)
    print(BANNER)
    print("  Stopping all SeisWork components…")
    n = _stop_all_seiswork(stop_service=True)
    if n == 0 and not _service_installed():
        print("  No running SeisWork components found.")
    print(f"  ✓ SeisWork stopped ({n} process(es) killed; ports freed).")
    if _service_installed():
        print("  (Service left stopped — start again with 'seiswork restart' or "
              "'seiswork service start'.)")


def cmd_restart(args):
    """Hard-restart so edited code actually reloads. Kills everything first.
    When installed as a service, restarts the SERVICE (killing PIDs alone gets
    respawned). --stop = stop only; --foreground = relaunch without the service."""
    try:
        import psutil  # noqa: F401
    except ImportError:
        print("  ✗ 'restart' needs psutil:  pip install psutil")
        sys.exit(1)
    import time
    print(BANNER)

    if getattr(args, "stop", False):
        n = _stop_all_seiswork(stop_service=True)
        print(f"  ✓ Stopped ({n} process(es)).")
        return

    svc = _service_installed()
    foreground = getattr(args, "foreground", False)

    if svc and not foreground:
        # Service-managed -> restart the service; reap stray processes first.
        print(f"  Restarting the SeisWork {svc} service (fresh code)…")
        _stop_all_seiswork(stop_service=False)          # kill runners/mirror/stray
        _clear_gui_state()
        time.sleep(0.6)
        label = _restart_seiswork_service()
        if label:
            time.sleep(1.5)
            running = _detect_running_gui()
            where = f" → {running['url']}" if running else ""
            print(f"  ✓ Restarted {label}{where}")
            print("  Open the window with:  seiswork open")
            return
        print("  ⚠ Service restart failed — falling back to a foreground launch.")

    # No service (or --foreground): kill everything, then relaunch in the foreground.
    print("  Stopping all SeisWork components…")
    n = _stop_all_seiswork(stop_service=True)
    print(f"  ✓ Stopped ({n} process(es); ports freed).")
    time.sleep(0.8)   # let the sockets free before rebinding
    print("  Relaunching GUI…\n")
    cmd_gui(args)


def cmd_open(args):
    """Open only the GUI window against an already-running server."""
    running = _detect_running_gui()
    if not running:
        msg = ("No SeisWork server is running.\n\n"
               "Start it with:\n    seiswork gui --native")
        _gui_alert("SeisWork is not running", msg)
        print("  ✗ No SeisWork server is running.")
        print("    Start one with:  seiswork gui --native")
        sys.exit(1)

    port, url = running["port"], running["url"]
    print(BANNER)
    print(f"  Attaching to the running SeisWork server → {url}")
    # Native window against the running server (no Flask started here).
    if _run_native_window("127.0.0.1", port, start_server=False):
        return
    # Native unavailable -> browser if there's a display, else just print the URL.
    import platform as _plat
    has_display = _plat.system() != "Linux" or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if has_display:
        import webbrowser, threading
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
        print(f"  Opened {url} in the browser.")
        return
    print("  The server is already running — open it in a browser:")
    print(f"      {url}")
    print(f"      http://localhost:{port}    (if this host/port is forwarded to you)")
    print(f"  (This machine has no desktop, so `seiswork open` can't show a native window.)")


def cmd_service(args):
    """Manage the SeisWork service (systemd on Linux, launchd on macOS)."""
    import subprocess, shutil, platform
    action = args.service_action
    port   = getattr(args, "port", 5000)

    # macOS → launchd LaunchAgent (reports port + IP on status).
    if platform.system() == "Darwin":
        _service_macos(action, args)
        return
    # Non-Linux without systemd → guide to running the GUI manually.
    if platform.system() != "Linux" or shutil.which("systemctl") is None:
        print("[service] systemd user services are only available on Linux.")
        print("          'systemctl' not found — start the GUI manually: seiswork gui")
        return

    if not _ensure_xdg_runtime():
        print("[service] ERROR: XDG_RUNTIME_DIR could not be set.")
        print("          /run/user/$(id -u) does not exist — make sure loginctl linger")
        print("          is active and log back in, or run from a full GUI/SSH session.")
        sys.exit(1)

    if action == "install":
        _SERVICE_DIR.mkdir(parents=True, exist_ok=True)
        unit = _service_unit(port)
        _SERVICE_FILE.write_text(unit)
        print(f"[service] unit file → {_SERVICE_FILE}")
        _systemctl(["daemon-reload"])
        _systemctl(["enable", _SERVICE_NAME])
        # Enable linger so user service starts without login
        subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")])
        print(f"[service] enabled (auto-start on boot)")
        _systemctl(["start", _SERVICE_NAME])
        print(f"[service] started")
        r = _systemctl(["is-active", _SERVICE_NAME], capture=True)
        status = r.stdout.strip()
        print(f"[service] status: {status}")
        if status == "active":
            for u in _service_urls(port):
                print(f"\n  SeisWork GUI  →  {u}")
        else:
            print("\n  [!] Check the log: journalctl --user -u seiswork -n 30")

    elif action == "remove":
        _systemctl(["stop",    _SERVICE_NAME])
        _systemctl(["disable", _SERVICE_NAME])
        if _SERVICE_FILE.exists():
            _SERVICE_FILE.unlink()
            print(f"[service] removed {_SERVICE_FILE}")
        _systemctl(["daemon-reload"])
        print("[service] seiswork service removed")

    elif action == "start":
        _systemctl(["start", _SERVICE_NAME])

    elif action == "stop":
        _systemctl(["stop", _SERVICE_NAME])

    elif action == "restart":
        _systemctl(["restart", _SERVICE_NAME])

    elif action == "enable":
        _systemctl(["enable", _SERVICE_NAME])
        print("[service] enabled — will auto-start on boot")

    elif action == "disable":
        _systemctl(["disable", _SERVICE_NAME])
        print("[service] disabled — will not auto-start on boot")

    elif action == "status":
        _systemctl(["status", _SERVICE_NAME])
        # Surface where the GUI is, so the operator sees the port + IP directly.
        p = _read_unit_port(port)
        print()
        for u in _service_urls(p):
            print(f"  SeisWork GUI  →  {u}")

    elif action == "log":
        n = getattr(args, "lines", 50)
        subprocess.run(["journalctl", "--user", "-u", _SERVICE_NAME, "-n", str(n), "--no-pager"])


# ── Remote (client ⇄ server federation) ─────────────────────────────────────────
def cmd_remote(args):
    """Drive a remote SeisWork server: connect, health, configs, run, jobs, log, sync."""
    import json as _json
    from seiswork.client import (client_from_config, save_connection,
                                 clear_connection, load_connection, SeisWorkClient)

    cfg = load_config()
    action = args.action

    # ── connection management (no live client needed) ──────────────────────────
    if action == "connect":
        url = (args.server or "").strip()
        if not url:
            print("[ERROR] connect needs --server URL"); sys.exit(1)
        try:
            probe = SeisWorkClient(url, args.token)
            h = probe.health()
        except Exception as e:
            print(f"[ERROR] Cannot reach {url}: {e}"); sys.exit(1)
        if h.get("auth_required") and not args.token:
            print("[ERROR] Server requires a token — retry with --token T"); sys.exit(1)
        save_connection(url, args.token)
        print(f"[remote] connected & saved → {url}")
        print(f"  server_id: {h.get('server_id')}  host: {h.get('hostname')}  v{h.get('version')}")
        print("  Now `seiswork remote <cmd>` uses this server without --server/--token.")
        return
    if action == "disconnect":
        clear_connection()
        print("[remote] saved connection cleared — back to local/standalone mode.")
        return

    try:
        cli = client_from_config(cfg, server_url=args.server, token=args.token,
                                 base_dir=str(BASE_DIR))
        if not cli.base:
            raise ValueError("no server")
    except ValueError:
        c = load_connection()
        hint = f" (saved: {c.get('server_url')})" if c.get("server_url") else ""
        print("[ERROR] No server configured. Run `seiswork remote connect --server URL "
              f"[--token T]` first{hint}, or pass --server.")
        sys.exit(1)

    if action == "info":
        try:
            d = cli.server_info()
        except Exception as e:
            print(f"[ERROR] {e}"); sys.exit(1)
        print(f"  server   : {cli.base}")
        print(f"  server_id: {d.get('server_id')}   host: {d.get('hostname')}")
        print(f"  urls     : {', '.join(d.get('urls', []) or [cli.base])}")
        print(f"  auth     : {'token required' if d.get('auth_required') else 'open'}")
        if d.get("token"):
            print(f"  token    : {d.get('token')}")
        elif d.get("auth_required"):
            print("  token    : (hidden — access from the server machine to view)")

    elif action == "pick":
        if not args.cfg:
            print("[ERROR] pick needs --cfg"); sys.exit(1)
        params = _json.loads(args.params) if args.params else {}
        res = cli.run_pick(args.cfg, method=args.method or "phasenet", params=params)
        print(f"[remote] pick job {res.get('id')} on {cli.base}")
        if args.wait:
            st = cli.wait(res.get("id"), on_log=lambda t: sys.stdout.write(t),
                          auto_sync=not args.no_sync)
            print(f"\n[remote] pick → {st.get('state')}")

    elif action == "health":
        try:
            h = cli.health()
        except Exception as e:
            print(f"[ERROR] Cannot reach {cli.base}: {e}")
            sys.exit(1)
        print(f"  server   : {cli.base}")
        print(f"  server_id: {h.get('server_id')}")
        print(f"  version  : {h.get('version')}   host: {h.get('hostname')}")
        print(f"  auth     : {'token required' if h.get('auth_required') else 'open (no token)'}")
        print(f"  time     : {h.get('time')}")

    elif action == "configs":
        for c in cli.list_configs():
            cid = c.get("id") or c.get("config_id") or "?"
            print(f"  {cid}  {c.get('name','')}  ({c.get('n_stations', '?')} sta)")

    elif action == "jobs":
        for j in cli.jobs(cfg_id=args.cfg or ""):
            print(f"  {j.get('id')}  {j.get('step','')}/{j.get('method','')}  "
                  f"{j.get('state','')}  events={j.get('events','-')}")

    elif action == "run":
        if not args.cfg or not args.step or not args.method:
            print("[ERROR] run needs --cfg, --step, --method")
            sys.exit(1)
        params = _json.loads(args.params) if args.params else {}
        res = cli.run(args.cfg, args.step, args.method, params=params,
                      input_file=args.input or "")
        jid = res.get("id")
        print(f"[remote] job {jid} started — {args.step}/{args.method} on {cli.base}")
        if args.wait:
            print("[remote] waiting (streaming log)…")
            st = cli.wait(jid, on_log=lambda t: sys.stdout.write(t),
                          auto_sync=not args.no_sync)
            print(f"\n[remote] job {jid} → {st.get('state')}  "
                  f"events={st.get('events','-')}")
            for p in st.get("_synced", []):
                print(f"  synced: {p}")

    elif action == "log":
        if not args.job:
            print("[ERROR] log needs --job ID"); sys.exit(1)
        lg = cli.job_log(args.job)
        print("\n".join(lg.get("lines", [])))
        print(f"\n[state: {lg.get('state','?')}]")

    elif action == "sync":
        if not args.job:
            print("[ERROR] sync needs --job ID"); sys.exit(1)
        written = cli.sync_job(args.job)
        print(f"[remote] synced {len(written)} file(s) for job {args.job}:")
        for p in written:
            print(f"  {p}")

    elif action == "registry":
        for jid, rec in cli.registered_jobs().items():
            print(f"  {jid}  {rec.get('step','')}/{rec.get('method','')}  "
                  f"server={rec.get('server','')}  mirror={rec.get('mirror','')}")

    elif action == "sync-token":
        if not args.cfg:
            print("[ERROR] sync-token needs --cfg"); sys.exit(1)
        try:
            tok = (cli.regen_sync_token(args.cfg) if args.regen
                   else cli.get_sync_token(args.cfg))
        except Exception as e:
            print(f"[ERROR] {e}"); sys.exit(1)
        if not tok:
            print("  (hidden — run from the server machine, or with the current "
                  "sync token, to view it)")
        else:
            verb = "Regenerated" if args.regen else "Sync token"
            print(f"  {verb} for cfg_id {args.cfg}: {tok}")
            print("  Give this token to a subscriber so they can pull only this "
                  "project via `seiswork sync --cfg "
                  f"{args.cfg} --sync-token {tok} ...` "
                  "— it grants no access to any other project on this server.")


# ── Sync command (pull server results to local PC) ────────────────────────────
def cmd_sync(args):
    """Pull pipeline results from a remote SeisWork server to local storage."""
    from seiswork.client import (client_from_config, load_connection)

    cfg = {}
    try:
        cfg = load_config()
    except SystemExit:
        pass

    try:
        cli = client_from_config(
            cfg,
            server_url=args.server,
            token=args.token,
            base_dir=str(BASE_DIR),
        )
        if not cli.base:
            raise ValueError("no server")
    except ValueError:
        c = load_connection()
        hint = (f" (saved: {c.get('server_url')})"
                if c.get("server_url") else "")
        print(f"[ERROR] No server configured{hint}. "
              "Run `seiswork remote connect --server URL` first, "
              "or use --server URL.")
        import sys as _sys; _sys.exit(1)

    if args.dir:
        from pathlib import Path as _Path
        cli.sync_dir = _Path(args.dir)

    action = args.sync_action

    # ── status ────────────────────────────────────────────────────────────────
    if action == "status":
        print(f"  server   : {cli.base}")
        try:
            manifest = cli.sync_manifest(args.cfg, sync_token=args.sync_token)
        except Exception as e:
            print(f"[ERROR] Failed to fetch manifest: {e}")
            import sys as _sys; _sys.exit(1)
        jobs      = manifest.get("jobs", [])
        server_id = manifest.get("server_id", "?")
        n_done    = sum(1 for j in jobs if j.get("state") == "done")
        n_running = sum(1 for j in jobs if j.get("state") == "running")
        print(f"  server_id: {server_id}")
        print(f"  total job: {len(jobs)}  (done={n_done}, running={n_running})")
        print(f"  sync dir : {cli.sync_dir}")
        print()
        if not jobs:
            print("  (no jobs on server)")
            return
        print(f"  {'ID':8}  {'KIND':8}  {'STEP':12}  {'METHOD':12}  "
              f"{'STATE':8}  {'EVENTS':>6}  {'SIZE MB':>7}")
        print("  " + "-" * 72)
        for j in jobs:
            mb = j.get("size_bytes", 0) / 1_000_000
            print(f"  {j['id'][:8]}  {j['kind'][:8]:<8}  "
                  f"{j.get('step','')[:12]:<12}  {j.get('method','')[:12]:<12}  "
                  f"{j.get('state','')[:8]:<8}  {j.get('events',0):>6}  {mb:>7.1f}")

    # ── pull ─────────────────────────────────────────────────────────────────
    elif action == "pull":
        job_ids = ([x.strip() for x in args.jobs.split(",") if x.strip()]
                   if args.jobs else None)
        kinds   = ([args.kind] if args.kind else None)
        dry     = args.dry_run
        force   = args.force

        tag = "DRY RUN — " if dry else ""
        print(f"[sync] {tag}pull from {cli.base}  →  {cli.sync_dir}")
        if job_ids:
            print(f"[sync] filter job_ids: {job_ids}")
        if kinds:
            print(f"[sync] filter kind: {kinds}")
        print()

        try:
            results = cli.sync_pull(args.cfg, job_ids=job_ids, dry_run=dry,
                                    force=force, kinds=kinds,
                                    sync_token=args.sync_token)
        except Exception as e:
            print(f"[ERROR] {e}")
            import sys as _sys; _sys.exit(1)

        pulled  = [r for r in results if r.get("action") in ("pull", "pulled")]
        skipped = [r for r in results if r.get("action") == "skip"]
        errors  = [r for r in results if r.get("action") == "error"]

        for r in pulled:
            if dry:
                mb = r.get("size_bytes", 0) / 1_000_000
                print(f"  AKAN DOWNLOAD  {r['id'][:8]}  {r['kind']:<8}  "
                      f"{r.get('n_files',0):>3} file  {mb:>6.1f} MB  "
                      f"[{r.get('state','')}]")
            else:
                print(f"  OK  {r['id'][:8]}  {r['kind']:<8}  → {r.get('path','?')}")

        for r in skipped:
            print(f"  --  {r['id'][:8]}  {r['kind']:<8}  (already up to date)")

        for r in errors:
            print(f"  ERR {r['id'][:8]}  {r['kind']:<8}  {r.get('error','?')}")

        print()
        if dry:
            total_mb = sum(r.get("size_bytes", 0) for r in pulled) / 1_000_000
            print(f"[sync] dry-run: {len(pulled)} job(s) would be downloaded "
                  f"({total_mb:.1f} MB total), {len(skipped)} skip, "
                  f"{len(errors)} error.")
            print("       Run without --dry-run to start downloading.")
        else:
            print(f"[sync] done: {len(pulled)} downloaded, "
                  f"{len(skipped)} skip, {len(errors)} error.")
            if pulled:
                print(f"[sync] hasil di: {cli.sync_dir}")


# ── Full pipeline ──────────────────────────────────────────────────────────────
def cmd_full(args):
    """Run the complete pipeline end-to-end."""
    print(BANNER)
    cfg = load_config()

    STEPS = [
        ("pick",      cmd_pick),
        ("associate", cmd_associate),
        ("velocity",  cmd_velocity),
        ("locate",    cmd_locate),
        ("magnitude", cmd_magnitude),
        ("relocate",  cmd_relocate),
    ]

    start = args.start or "pick"
    end   = args.end   or "relocate"

    step_names = [s[0] for s in STEPS]
    try:
        i_start = step_names.index(start)
        i_end   = step_names.index(end) + 1
    except ValueError as e:
        print(f"[ERROR] Unknown step: {e}")
        sys.exit(1)

    selected = STEPS[i_start:i_end]
    print(f"[Pipeline] Running {len(selected)} steps: {' → '.join(s[0] for s in selected)}")
    print()

    # Build default args namespace for each sub-command
    pick_args      = argparse.Namespace(method=args.pick_method or "phasenet",  workers=args.workers or 4)
    assoc_args     = argparse.Namespace(method=args.assoc_method or "gamma",    picks=None)
    velocity_args  = argparse.Namespace(phases=None, mode=1)
    locate_args    = argparse.Namespace(method=args.locate_method or "nlloc",   catalog=None)
    magnitude_args = argparse.Namespace(catalog=None, inventory=None)
    relocate_args  = argparse.Namespace(method=args.relocate_method or "catalog", catalog=None)

    step_args = {
        "pick":      pick_args,
        "associate": assoc_args,
        "velocity":  velocity_args,
        "locate":    locate_args,
        "magnitude": magnitude_args,
        "relocate":  relocate_args,
    }

    for name, func in selected:
        print(f"\n{'='*60}")
        print(f"  STEP: {name.upper()}")
        print(f"{'='*60}")
        func(step_args[name])

    print(f"\n[Done] Pipeline completed. Results in: {WORK_DIR}")


# ── Argument parser ────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seiswork",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""\
            SeisWork — Simple Seismological Data Processing Framework
            by HakimBMKG

            Pipeline:
              pick → associate → velocity → locate → magnitude → relocate
        """),
        epilog=textwrap.dedent("""\
            Examples:
              python seiswork.py info
              python seiswork.py setup
              python seiswork.py pick --method phasenet --workers 8
              python seiswork.py pick --method stalta
              python seiswork.py associate --method gamma
              python seiswork.py associate --method real
              python seiswork.py velocity --mode 0
              python seiswork.py locate --method nlloc
              python seiswork.py locate --method hypoinverse
              python seiswork.py locate --method all
              python seiswork.py magnitude
              python seiswork.py relocate --method catalog
              python seiswork.py relocate --method crosscorr
              python seiswork.py relocate --method all
              python seiswork.py plot --step all
              python seiswork.py full
              python seiswork.py full --start associate --end relocate
              python seiswork.py gui
              python seiswork.py gui --port 8080
        """),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # info
    p_info = sub.add_parser("info", help="Show tool availability and pipeline overview")
    p_info.set_defaults(func=cmd_info)

    # setup
    p_setup = sub.add_parser("setup", help="Initialize workspace and working directories")
    p_setup.set_defaults(func=cmd_setup)

    # pick
    p_pick = sub.add_parser("pick", help="Run phase picker on waveform data")
    p_pick.add_argument("--method",  default="phasenet", choices=["phasenet","stalta","all"],
                        help="Picker method (default: phasenet)")
    p_pick.add_argument("--workers", type=int, default=4,
                        help="Parallel workers for PhaseNet (default: 4)")
    p_pick.set_defaults(func=cmd_pick)

    # associate
    p_assoc = sub.add_parser("associate", help="Run phase association (picks → events)")
    p_assoc.add_argument("--method", default="gamma", choices=["gamma","real","all"],
                         help="Association method (default: gamma)")
    p_assoc.add_argument("--picks",  default=None, metavar="FILE",
                         help="Input picks CSV (default: work/picks/picks.csv)")
    p_assoc.set_defaults(func=cmd_associate)

    # velocity
    p_vel = sub.add_parser("velocity", help="Estimate 1D local velocity model (VELEST)")
    p_vel.add_argument("--phases", default=None, metavar="FILE",
                       help="Input phase file (default: work/catalog/phases.pha)")
    p_vel.add_argument("--mode",   type=int, default=1, choices=[0, 1],
                       help="0=full inversion (vel+loc), 1=locations only (default: 1)")
    p_vel.set_defaults(func=cmd_velocity)

    # locate
    p_loc = sub.add_parser("locate", help="Compute hypocenter locations")
    p_loc.add_argument("--method",  default="nlloc",
                       choices=["hypoinverse","locsat","nlloc","all"],
                       help="Location method (default: nlloc)")
    p_loc.add_argument("--catalog", default=None, metavar="FILE",
                       help="Input associated catalog CSV")
    p_loc.set_defaults(func=cmd_locate)

    # magnitude
    p_mag = sub.add_parser("magnitude", help="Compute local magnitude ML")
    p_mag.add_argument("--catalog",   default=None, metavar="FILE",
                       help="Input located catalog CSV")
    p_mag.add_argument("--inventory", default=None, metavar="FILE",
                       help="StationXML inventory (PAZ response)")
    p_mag.set_defaults(func=cmd_magnitude)

    # relocate
    p_rel = sub.add_parser("relocate", help="Run relative relocation (HypoDD / GrowClust)")
    p_rel.add_argument("--method",  default="catalog",
                       choices=["catalog","crosscorr","growclust","all"],
                       help="catalog=ph2dt+hypoDD, crosscorr=waveform CC, "
                            "growclust=GrowClust relative reloc (default: catalog)")
    p_rel.add_argument("--catalog", default=None, metavar="FILE",
                       help="Input located catalog CSV")
    p_rel.set_defaults(func=cmd_relocate)

    # detect (template matching)
    p_det = sub.add_parser("detect", help="Template-matching detection (Match&Locate)")
    p_det.add_argument("--method",  default="matchlocate",
                       choices=["matchlocate","all"],
                       help="matchlocate = MatchLocate2 template detection (default)")
    p_det.add_argument("--catalog", default=None, metavar="FILE",
                       help="Template catalog CSV (default: best available in work/catalog)")
    p_det.set_defaults(func=cmd_detect)

    # plot
    p_plot = sub.add_parser("plot", help="Generate maps and figures")
    p_plot.add_argument("--step", default="all",
                        choices=["pick","associate","locate","relocate","all"],
                        help="Which step to plot (default: all)")
    p_plot.set_defaults(func=cmd_plot)

    # full
    p_full = sub.add_parser("full", help="Run complete pipeline end-to-end")
    p_full.add_argument("--start",          default="pick",
                        choices=["pick","associate","velocity","locate","magnitude","relocate"],
                        help="First step to run (default: pick)")
    p_full.add_argument("--end",            default="relocate",
                        choices=["pick","associate","velocity","locate","magnitude","relocate"],
                        help="Last step to run (default: relocate)")
    p_full.add_argument("--pick-method",    default="phasenet",
                        choices=["phasenet","stalta","all"], dest="pick_method")
    p_full.add_argument("--assoc-method",   default="gamma",
                        choices=["gamma","real","all"], dest="assoc_method")
    p_full.add_argument("--locate-method",  default="nlloc",
                        choices=["hypoinverse","locsat","nlloc","all"], dest="locate_method")
    p_full.add_argument("--relocate-method",default="catalog",
                        choices=["catalog","crosscorr","all"], dest="relocate_method")
    p_full.add_argument("--workers",        type=int, default=4)
    p_full.set_defaults(func=cmd_full)

    # gui
    p_gui = sub.add_parser("gui", help="Launch web GUI (Leaflet map + station config)")
    p_gui.add_argument("--host",  default="0.0.0.0",
                       help="Server host (default: 0.0.0.0 — accessible from LAN; use 127.0.0.1 for local only)")
    p_gui.add_argument("--port",  type=int, default=None,
                       help="Server port (default: auto — tries 5000, then the next "
                            "free port; pass a value to pin it)")
    p_gui.add_argument("--debug", action="store_true",
                       help="Enable Flask debug/auto-reload mode")
    p_gui.add_argument("--no-browser", action="store_true", dest="no_browser",
                       help="Do not open browser automatically (used by systemd service)")
    p_gui.add_argument("--native", action="store_true",
                       help="Open in a native desktop window (pywebview / WKWebView) "
                            "instead of a browser tab — much lighter on RAM/CPU. Falls "
                            "back to the browser if pywebview is not installed.")
    p_gui.add_argument("--force", action="store_true",
                       help="Stop any already-running SeisWork server first, then start "
                            "(avoids a stale second server serving old code)")
    p_gui.set_defaults(func=cmd_gui)

    # restart — stop every running SeisWork component, then relaunch the GUI fresh
    p_restart = sub.add_parser("restart",
                       help="Hard-restart: stop all SeisWork components (GUI, web, agent) and relaunch")
    p_restart.add_argument("--host", default="0.0.0.0",
                           help="Server host for the relaunched GUI (default: 0.0.0.0)")
    p_restart.add_argument("--port", type=int, default=None,
                           help="Server port for the relaunched GUI (default: auto-detect a free port)")
    # restart opens the NATIVE desktop window by default; --browser opts out.
    p_restart.add_argument("--browser", action="store_false", dest="native",
                           help="Relaunch in the browser instead of the native window")
    p_restart.add_argument("--no-browser", action="store_true", dest="no_browser",
                           help="Do not open a browser after relaunch")
    p_restart.add_argument("--debug", action="store_true",
                           help="Enable Flask debug/auto-reload after relaunch")
    p_restart.add_argument("--stop", action="store_true",
                           help="Only stop the components; do not relaunch")
    p_restart.add_argument("--foreground", action="store_true",
                           help="Relaunch in the foreground (native GUI) instead of "
                                "restarting the launchd/systemd service")
    p_restart.set_defaults(func=cmd_restart, force=True, native=True)

    # stop — kill EVERYTHING (service + servers + runners + slarchive), free ports
    p_stop = sub.add_parser("stop",
                       help="Stop everything: service, GUI/mirror/agent servers, pipelines, ports")
    p_stop.set_defaults(func=cmd_stop)

    # open — attach a native window to an ALREADY-running server (no new server)
    p_open = sub.add_parser("open",
                       help="Open only the GUI window against an already-running SeisWork server")
    p_open.set_defaults(func=cmd_open)

    # service
    p_svc = sub.add_parser("service",
                           help="Manage SeisWork systemd user service (auto-start on boot)")
    p_svc.add_argument("service_action",
                       choices=["install", "remove", "start", "stop",
                                "restart", "enable", "disable", "status", "log"],
                       help="install=setup+enable+start, remove=stop+uninstall, "
                            "start/stop/restart/status=control, "
                            "enable/disable=autostart toggle, log=recent journal")
    p_svc.add_argument("--port",  type=int, default=5000,
                       help="GUI port (install only; default: 5000)")
    p_svc.add_argument("--lines", type=int, default=50,
                       help="Log lines to show (log only; default: 50)")
    p_svc.set_defaults(func=cmd_service)

    # remote (client ⇄ server)
    p_rem = sub.add_parser("remote",
                           help="Drive a remote SeisWork server (client mode)")
    p_rem.add_argument("action",
                       choices=["connect", "disconnect", "info", "health",
                                "configs", "jobs", "pick", "run", "log",
                                "sync", "registry", "sync-token"],
                       help="connect/disconnect=save/remove server, info=identity+token, "
                            "health=ping, configs/jobs=list, pick/run=submit job, "
                            "log=fetch log, sync=mirror artifacts, registry=job↔server map, "
                            "sync-token=view/regenerate one project's federation sync token "
                            "(--cfg required; --regen to rotate it)")
    p_rem.add_argument("--server", default="", metavar="URL",
                       help="Server base URL (e.g. http://host:5000); "
                            "default = federation.server_url / SEISWORK_SERVER")
    p_rem.add_argument("--token",  default="", metavar="T",
                       help="Bearer token (default = federation.token / SEISWORK_TOKEN)")
    p_rem.add_argument("--cfg",    default="", help="Config id (run/jobs/sync-token)")
    p_rem.add_argument("--step",   default="", help="Pipeline step (run)")
    p_rem.add_argument("--method", default="", help="Step method (run)")
    p_rem.add_argument("--params", default="", help="JSON params for the step (run)")
    p_rem.add_argument("--input",  default="", help="Input catalog/picks file (run)")
    p_rem.add_argument("--job",    default="", help="Job id (log/sync)")
    p_rem.add_argument("--wait",   action="store_true",
                       help="run: block, stream log, auto-sync results on finish")
    p_rem.add_argument("--no-sync", action="store_true",
                       help="run --wait: do not auto-sync artifacts")
    p_rem.add_argument("--regen",  action="store_true",
                       help="sync-token: rotate the token instead of just viewing it")
    p_rem.set_defaults(func=cmd_remote)

    # sync — pull server results to local PC
    p_sync = sub.add_parser(
        "sync",
        help="Synchronize pipeline results from remote server to local PC",
        description=textwrap.dedent("""\
            Pull job results from a SeisWork server to a local directory.
            Use 'sync status' to list jobs on the server,
            then 'sync pull' to download new or changed results.
        """),
    )
    p_sync.add_argument("sync_action", choices=["status", "pull"],
                        help="status=list jobs on server, pull=download results")
    p_sync.add_argument("--cfg", default="", required=True,
                        help="Config id to sync (required — sync is scoped to "
                             "one project; see `seiswork remote sync-token`)")
    p_sync.add_argument("--server", default="",
                        help="Server URL (default = saved connection)")
    p_sync.add_argument("--token",  default="",
                        help="Bearer token (default = saved token)")
    p_sync.add_argument("--sync-token", default="", dest="sync_token",
                        help="This project's federation sync token (default = "
                             "--token; fetch with `seiswork remote sync-token "
                             "--cfg ID`)")
    p_sync.add_argument("--dir",    default="",  metavar="PATH",
                        help="Local destination directory for sync "
                             "(default: ~/.seiswork/remote/)")
    p_sync.add_argument("--jobs",   default="",  metavar="ID1,ID2",
                        help="pull: only download specific job IDs (comma-separated)")
    p_sync.add_argument("--kind",   default="",
                        choices=["pipeline", "picking", ""],
                        help="pull: filter by kind (default: all)")
    p_sync.add_argument("--dry-run", action="store_true",
                        help="pull: show what would be downloaded "
                             "without actually downloading")
    p_sync.add_argument("--force",  action="store_true",
                        help="pull: force re-download even if already up to date")
    p_sync.set_defaults(func=cmd_sync)

    return parser


# ── Entry point (called by console_scripts in pyproject.toml) ─────────────────
def main():
    parser = build_parser()
    if len(sys.argv) == 1:
        print(BANNER)
        parser.print_help()
        sys.exit(0)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

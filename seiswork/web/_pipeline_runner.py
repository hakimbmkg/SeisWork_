#!/usr/bin/env python3
"""
SeisWork pipeline step runner - by HakimBMKG

Run as a subprocess by Flask's _pipe_worker().
The if __name__ == "__main__" guard is mandatory.

Signature:
  python _pipeline_runner.py <cfg_yaml> <base_dir> <step> <method> <job_id> [input_file]

step    : assoc | locate | magnitude | velocity | relocation
method  : gamma | real | pyocto | glass3 | nlloc | locsat | ml | velest | hypodd
"""
import sys
import traceback
from pathlib import Path


if __name__ == "__main__":
    cfg_yaml   = sys.argv[1]
    base_dir   = Path(sys.argv[2])
    step       = sys.argv[3]
    method     = sys.argv[4]
    job_id     = sys.argv[5]
    input_file = sys.argv[6] if len(sys.argv) > 6 else ""

    print(f"[runner] base_dir={base_dir}", flush=True)
    print(f"[runner] step={step}  method={method}", flush=True)
    print(f"[runner] input={input_file}", flush=True)

    import yaml
    with open(cfg_yaml) as fh:
        cfg = yaml.safe_load(fh)

    job_dir = Path(cfg["data"].get("job_dir", str(base_dir / "work" / step)))

    # Copy picks from the input catalog dir into this job_dir so downstream
    # steps (NLLoc obs, HypoDD phase.dat) find picks alongside this step's
    # output catalog.
    if input_file:
        import shutil
        src_dir = Path(input_file).resolve().parent
        for nm in ("picks_associated.csv", "picks_gamma.csv", "picks_real.csv"):
            s = src_dir / nm
            if s.exists():
                job_dir.mkdir(parents=True, exist_ok=True)
                if not (job_dir / "picks_associated.csv").exists():
                    shutil.copy2(s, job_dir / "picks_associated.csv")
                    print(f"[runner] picks copied alongside: {s}", flush=True)
                break

    # ── Optional magnitude (merged into assoc/locate) ─────────────────────────
    # If cfg["magnitude"]["compute"], compute ML on the catalog from this step
    # and write back the `mag` column in place (file name does not change).
    def _maybe_compute_magnitude(cat_names):
        mcfg = cfg.get("magnitude", {})
        if not mcfg.get("compute"):
            return
        cat_path = None
        for nm in cat_names:
            p = Path(job_dir) / nm
            if p.exists():
                cat_path = p; break
        if not cat_path:
            print("[runner] magnitude: catalog not found, skipping", flush=True)
            return
        try:
            import pandas as pd
            from seiswork.modules.magnitude.ml import MLMagnitude
            print(f"[runner] Computing ML for {cat_path.name} ...", flush=True)
            mag = MLMagnitude(cfg, base_dir)
            mag.out_dir = str(job_dir)
            mag.cat_dir = str(job_dir)
            mag.run(str(cat_path), inventory_file=mcfg.get("inventory", "")
                    or cfg.get("data", {}).get("inventory", ""))
            ml_path = Path(job_dir) / "catalog_ml.csv"
            if ml_path.exists():
                orig = pd.read_csv(cat_path)
                mldf = pd.read_csv(ml_path)
                if "mag" in mldf.columns and len(orig) == len(mldf):
                    orig["mag"] = mldf["mag"].values
                    orig.to_csv(cat_path, index=False)
                    print(f"[runner] ML digabung ke {cat_path.name} "
                          f"({orig['mag'].notna().sum()} event)", flush=True)
        except Exception as e:
            print(f"[runner] WARNING: magnitude computation failed: {e}", flush=True)

    try:
        # ── Association ────────────────────────────────────────────────────────
        if step == "assoc":
            if method == "gamma":
                from seiswork.modules.associator.gamma import GammaAssociator
                a = GammaAssociator(cfg, base_dir)
                a.catalog_dir = str(job_dir)
                a.run(input_file)

            elif method == "real":
                from seiswork.modules.associator.real import RealAssociator
                a = RealAssociator(cfg, base_dir)
                a.cat_dir  = str(job_dir)
                a.real_dir = str(job_dir / "real_tmp")
                a.log_dir  = str(job_dir / "logs" / "real")
                a.run(input_file)

            elif method == "pyocto":
                from seiswork.modules.associator.pyocto import PyOctoAssociator
                a = PyOctoAssociator(cfg, base_dir)
                a.catalog_dir = str(job_dir)
                a.run(input_file)

            elif method == "glass3":
                from seiswork.modules.associator.glass3 import Glass3Associator
                a = Glass3Associator(cfg, base_dir)
                a.cat_dir   = str(job_dir)
                a.glass_dir = str(job_dir / "glass3_tmp")
                a.log_dir   = str(job_dir / "logs" / "glass3")
                a.run(input_file)

            else:
                print(f"[runner] ERROR: unknown assoc method: {method}")
                sys.exit(1)

            # Write HypoDD phase.dat for downstream chaining
            try:
                import pandas as pd
                from seiswork.utils.converter import catalog_picks_to_hypodd_phase
                cat_candidates = ['catalog_associated.csv', 'catalog_gamma.csv', 'catalog_real.csv']
                pick_candidates = ['picks_associated.csv', 'picks_gamma.csv', 'picks_real.csv']
                cat_path = None
                for nm in cat_candidates:
                    p = Path(job_dir) / nm
                    if p.exists():
                        cat_path = str(p); break
                pick_path = None
                for nm in pick_candidates:
                    p = Path(job_dir) / nm
                    if p.exists():
                        pick_path = str(p); break
                if cat_path and pick_path:
                    cat_df = pd.read_csv(cat_path)
                    pick_df = pd.read_csv(pick_path)
                    phase_out = str(Path(job_dir) / 'hypodd_phase.pha')
                    catalog_picks_to_hypodd_phase(cat_df, pick_df, phase_out)
                    print(f"[runner] HypoDD phase: {phase_out} ({len(cat_df)} events)", flush=True)
            except Exception as e:
                print(f"[runner] WARNING: HypoDD phase export failed: {e}", flush=True)

            _maybe_compute_magnitude(['catalog_associated.csv', 'catalog_gamma.csv', 'catalog_real.csv'])

        # ── Location ───────────────────────────────────────────────────────────
        elif step == "locate":
            if method == "nlloc":
                from seiswork.modules.locator.nlloc import NLLocLocator
                # Station list lets NLLocLocator auto-build a travel-time grid
                # (sized to the network's spread) when none exists at the
                # configured grid_dir, instead of requiring a pre-built one
                # (see NLLocLocator._ensure_grid).
                stations = None
                try:
                    inv_path = (cfg.get("data", {}).get("inventory")
                               or str(base_dir / "config" / "inventory.xml"))
                    if Path(inv_path).exists():
                        from seiswork.web._online_stations import get_stations
                        stations = get_stations(inv_path)
                except Exception:
                    pass
                l = NLLocLocator(cfg, base_dir, stations=stations)
                l.catalog_dir = str(job_dir)
                l.out_dir     = str(job_dir / "nlloc_work")
                l.run(input_file)

            elif method == "locsat":
                from seiswork.modules.locator.locsat import LocSATLocator
                l = LocSATLocator(cfg, base_dir)
                l.out_dir = str(job_dir)
                l.run(input_file)

            else:
                print(f"[runner] ERROR: unknown locate method: {method}")
                sys.exit(1)

            # Write HypoDD phase.dat for downstream chaining
            try:
                import pandas as pd
                from seiswork.utils.converter import catalog_picks_to_hypodd_phase
                cat_candidates = ['catalog_nlloc.csv', 'catalog_locsat.csv', 'catalog_located.csv']
                pick_candidates = ['picks_associated.csv', 'picks_gamma.csv', 'picks_real.csv']
                cat_path = None
                for nm in cat_candidates:
                    p = Path(job_dir) / nm
                    if p.exists():
                        cat_path = str(p); break
                pick_path = None
                for nm in pick_candidates:
                    p = Path(job_dir) / nm
                    if p.exists():
                        pick_path = str(p); break
                if cat_path and pick_path:
                    cat_df = pd.read_csv(cat_path)
                    pick_df = pd.read_csv(pick_path)
                    phase_out = str(Path(job_dir) / 'hypodd_phase.pha')
                    catalog_picks_to_hypodd_phase(cat_df, pick_df, phase_out)
                    print(f"[runner] HypoDD phase: {phase_out} ({len(cat_df)} events)", flush=True)
            except Exception as e:
                print(f"[runner] WARNING: HypoDD phase export failed: {e}", flush=True)

            _maybe_compute_magnitude(['catalog_nlloc.csv', 'catalog_locsat.csv', 'catalog_located.csv'])

        # ── Magnitude ──────────────────────────────────────────────────────────
        elif step == "magnitude":
            from seiswork.modules.magnitude.ml import MLMagnitude
            mag = MLMagnitude(cfg, base_dir)
            mag.out_dir  = str(job_dir)
            mag.cat_dir  = str(job_dir)
            inventory = cfg["data"].get("inventory", "")
            mag.run(input_file, inventory_file=inventory)

        # ── Velocity ───────────────────────────────────────────────────────────
        elif step == "velocity":
            from seiswork.modules.velocity.velest import VelestVelocity
            vel = VelestVelocity(cfg, base_dir)
            vel.vel_dir = str(job_dir)
            mode = int(cfg.get("velocity", {}).get("velest", {}).get("mode", 1))
            vel.run(input_file, mode=mode)

        # ── Imaging (SIMUL2000 tomography) ───────────────────────────────────────
        elif step == "imaging":
            import os as _os
            from seiswork.modules.velocity.tomography import Simul2000Tomography
            tomo = Simul2000Tomography(cfg, base_dir)
            tomo.tomo_dir = str(job_dir)
            tomo.work_dir = str(job_dir / "simulout")
            _os.makedirs(tomo.work_dir, exist_ok=True)
            icfg = cfg.get("imaging", {}).get("simul2000", {})
            tomo.run(
                eqks_source=icfg.get("eqks_source", "velest"),
                checkerboard=bool(icfg.get("checkerboard", False)),
            )

        # ── Mechanism (SKHASH first-motion + FocoNet DL) ──
        elif step == "mechanism":
            from seiswork.modules.mechanism.skhash_runner import FocalMechanism
            from seiswork.modules.mechanism.polarity import compute_polarities
            fm = FocalMechanism(cfg, base_dir)
            fm.work_dir = str(job_dir)
            mcfg = cfg.get("mechanism", {}).get("skhash", {})
            picks_csv = job_dir / "picks_associated.csv"
            if not input_file or not picks_csv.exists():
                raise RuntimeError(
                    "mechanism step needs an Assoc & Location catalog as input "
                    "(catalog_associated.csv with picks_associated.csv alongside)")
            inv_path = (cfg.get("data", {}).get("inventory")
                        or str(base_dir / "config" / "inventory.xml"))
            sds_path = (cfg.get("data", {}).get("waveform_dir", "")
                        or cfg.get("data", {}).get("sds_path", ""))
            network = mcfg.get("network", "7G")
            channel = mcfg.get("channel") or None  # None = auto HH > BH > EH > SH per station

            import pandas as _pd
            picks_df = _pd.read_csv(str(picks_csv))
            # Associators disagree on the phase-type column name: GaMMA writes
            # "type", REAL writes "phase". Normalise before filtering P/S.
            phase_col = "phase" if "phase" in picks_df.columns else "type"
            picks_p = picks_df[picks_df[phase_col] == "P"].copy()
            picks_s = picks_df[picks_df[phase_col] == "S"].copy()
            if "network" not in picks_p.columns:
                picks_p["network"] = network
            if "network" not in picks_s.columns:
                picks_s["network"] = network
            # compute_polarities()/FocoNet need [event_id, network, station,
            # pick_time]. GaMMA's picks_associated.csv uses "timestamp"/
            # "event_index" instead, so normalise both P and S frames the same way.
            for _pf in (picks_p, picks_s):
                if "pick_time" not in _pf.columns and "timestamp" in _pf.columns:
                    _pf["pick_time"] = _pf["timestamp"]
            if "event_id" not in picks_p.columns and "event_index" in picks_p.columns:
                picks_p["event_id"] = picks_p["event_index"]
                # Drop unassociated picks (event_index == -1): keeping them
                # creates many same-station rows under one fake "event -1",
                # which SKHASH rejects as duplicate measurements.
                picks_p = picks_p[picks_p["event_id"] >= 0].copy()
            if "event_id" not in picks_s.columns and "event_index" in picks_s.columns:
                picks_s["event_id"] = picks_s["event_index"]
                picks_s = picks_s[picks_s["event_id"] >= 0].copy()
            print("[MECH] pre-computing polarities for SKHASH + FocoNet...",
                  flush=True)
            pol_df = compute_polarities(picks_p, sds_path, channel=channel)

            # ── SKHASH ──────────────────────────────────────────────────────
            print("[MECH] running SKHASH...", flush=True)
            fm.run(
                catalog_csv=input_file,
                picks_csv=str(picks_csv),
                sds_path=sds_path,
                inventory_path=inv_path,
                network=network,
                channel=channel,
                _pol_df=pol_df,
            )

            # ── FocoNet (Full when S picks are available, else O) ────────────
            print("[MECH] running FocoNet (auto-select Full/O)...", flush=True)
            try:
                from seiswork.modules.mechanism.foconet_runner import FocoNetRunner
                fn = FocoNetRunner(cfg, base_dir)
                fn.out_dir = str(job_dir / "foconetout")
                import os as _os
                _os.makedirs(fn.out_dir, exist_ok=True)
                fn.run(
                    catalog_csv=input_file,
                    picks_csv=str(picks_csv),
                    sds_path=sds_path,
                    inventory_path=inv_path,
                    network=network,
                    channel=channel,
                    pol_df=pol_df,
                    picks_p_df=picks_p,
                    picks_s_df=picks_s if len(picks_s) > 0 else None,
                )
            except Exception as _e:
                import traceback as _tb
                print(f"[FOCONET] warning: FocoNet run failed: {_e}",
                      flush=True)
                _tb.print_exc()

        # ── Relocation ─────────────────────────────────────────────────────────
        elif step == "relocation":
            mode = cfg.get("relocation", {}).get("hypodd", {}).get("mode", "catalog")
            if mode == "growclust":
                from seiswork.modules.relocation.growclust import GrowClustRelocation
                rel = GrowClustRelocation(cfg, base_dir)
                rel.out_dir = str(job_dir)
                for sub in ("IN", "OUT", "TT"):
                    (job_dir / sub).mkdir(parents=True, exist_ok=True)
                rel.run_catalog(input_file)
            else:
                from seiswork.modules.relocation.hypodd import HypoDDRelocation
                rel = HypoDDRelocation(cfg, base_dir)
                if mode == "crosscorr":
                    rel.cc_out_dir  = str(job_dir)
                    rel.run_crosscorr(input_file)
                else:
                    rel.cat_out_dir = str(job_dir)
                    rel.run_catalog(input_file)

        # ── Detection (template matching) ────────────────────────────────────────
        elif step == "detect":
            from seiswork.modules.detection.matchlocate import MatchLocateDetection
            det = MatchLocateDetection(cfg, base_dir)
            det.out_dir  = str(job_dir)
            det.data_dir = str(job_dir / "Data")
            det.tmpl_dir = str(job_dir / "Template")
            import os as _os
            for sub in ("Data", "Template", _os.path.join("Template", "INPUT")):
                (job_dir / sub).mkdir(parents=True, exist_ok=True)
            det.run(input_file)

        else:
            print(f"[runner] ERROR: unknown step: {step}")
            sys.exit(1)

        print(f"[runner] DONE: {step}/{method} completed.", flush=True)

    except Exception:
        print(f"[runner] ERROR: exception in {step}/{method}", flush=True)
        traceback.print_exc()
        sys.exit(2)

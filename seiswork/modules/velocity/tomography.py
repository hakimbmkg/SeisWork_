#!/usr/bin/env python3
"""
SeisWork - SIMUL2000 local-earthquake tomography (imaging) - by HakimBMKG

Pipeline (adapted from SimulFlow / 7G_Jailolo.ipynb):
  1. Source events:
       - VELEST  : velest.mod (1D model), velest.sta, final.CNV   (velocity step)
       - HypoDD  : hypoDD.reloc + velesttohypo.pha                (relocation step)
  2. Build SIMUL2000 input set in work/imaging/simulout/:
       STNS  (stations)   <- Velest2simul2000.convert_stations
       EQKS  (events)     <- Velest2simul2000.convert_cnv_to_eqks | Hypodd2simul2000
       MOD   (3D grid)    <- Velest2simul2000.create_simul_mod
       CNTL  (control)    <- Velest2simul2000.create_cntl
  3. SimulRun.run()  -> velomod.out, output (DWS), finalsmpout
  4. Plots (PNG, Agg backend): parameterization, velocity, DWS, [checkerboard]

Reference:
  Thurber & Eberhart-Phillips (1999), Comp. & Geosci., 25, 809-818
  https://doi.org/10.5281/zenodo.5547889
"""

import os
import sys
import glob
import shutil
import time
from pathlib import Path

# Headless rendering: plotter calls plt.show() (no-op under Agg) after savefig.
import matplotlib
matplotlib.use("Agg")


# ── Jailolo / Halmahera defaults (from 7G_Jailolo.ipynb cell 19) ─────────────
DEFAULT_GRID = {
    "x_nodes": [-80, -70, -60, -50, -40, -30, -20, -10, 0,
                10, 20, 30, 40, 50, 60, 70, 80],
    "y_nodes": [-80, -70, -60, -50, -40, -30, -20, -10, 0,
                10, 20, 30, 40, 50, 60, 70, 80],
    "z_nodes": [-3.0, 0.0, 1.0, 3.0, 5.0, 10.0, 15.0, 20.0,
                25.0, 30.0, 40.0, 60.0],
    "vp_vals": [4.30, 4.72, 5.60, 5.70, 5.70, 5.74, 5.97, 6.47,
                7.98, 8.05, 8.10, 8.10],
    "vpvs": 1.716,
    "bld": 0.1,
}
DEFAULT_REF = {"lat": 1.1, "lon": 127.5, "elev": 0.0}
DEFAULT_DAMP = {"damp_p": 15.0, "damp_s": 10.0, "hitct": 5.0}


class Simul2000Tomography:
    """Prepare and run SIMUL2000 local-earthquake tomography."""

    def __init__(self, cfg: dict, base_dir):
        self.cfg = cfg
        self.base_dir = str(base_dir)
        self.tcfg = cfg.get("imaging", {}).get("simul2000", {})
        self.reg = cfg.get("region", {})

        self.tomo_dir = os.path.join(self.base_dir, "work", "imaging")
        os.makedirs(self.tomo_dir, exist_ok=True)
        # SIMUL2000 reads/writes relative to its CWD, so keep one working dir.
        self.work_dir = os.path.join(self.tomo_dir, "simulout")
        os.makedirs(self.work_dir, exist_ok=True)

        # SimulFlow package path (same convention as velest.py)
        self.simulflow_src = os.path.join(
            self.base_dir, "core", "simulflow", "src")
        if not os.path.isdir(self.simulflow_src):
            self.simulflow_src = os.path.join(
                os.path.expanduser("~"), "apps", "simulflow", "src")
        if self.simulflow_src not in sys.path:
            sys.path.insert(0, self.simulflow_src)

        # Resolved grid / reference / damping (cfg overrides defaults)
        self.grid = {k: self.tcfg.get(k, v) for k, v in DEFAULT_GRID.items()}
        self.ref = {
            "lat": self.tcfg.get("ref_lat", self.reg.get("lat", DEFAULT_REF["lat"])),
            "lon": self.tcfg.get("ref_lon", self.reg.get("lon", DEFAULT_REF["lon"])),
            "elev": self.tcfg.get("ref_elev", DEFAULT_REF["elev"]),
        }
        self.damp = {k: self.tcfg.get(k, v) for k, v in DEFAULT_DAMP.items()}

    # ── Derive Z layers + initial Vp from a VELEST 1D model (velest.mod) ───────
    @staticmethod
    def _parse_velest_mod(path):
        """Parse velest.mod -> (z_nodes, vp_vals, vpvs). Format:
              <title>
              <nlayers>  ...
              vp  depth  vdamp [phase]   x nlayers      (P-velocity block)
              <nlayers>
              vs  depth  vdamp [phase]   x nlayers      (S-velocity block)
        """
        with open(path) as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
        if len(lines) < 3:
            return None
        try:
            n = int(lines[1].split()[0])
        except (ValueError, IndexError):
            return None
        p_block = lines[2:2 + n]
        z, vp = [], []
        for ln in p_block:
            parts = ln.split()
            if len(parts) < 2:
                continue
            vp.append(float(parts[0]))
            z.append(float(parts[1]))
        if not z:
            return None
        # S-velocity block (optional), used for mean Vp/Vs
        vpvs = None
        s_start = 2 + n + 1                       # skip the repeated <nlayers> line
        s_block = lines[s_start:s_start + n]
        vs = []
        for ln in s_block:
            parts = ln.split()
            if len(parts) >= 1:
                try:
                    vs.append(float(parts[0]))
                except ValueError:
                    pass
        if len(vs) == len(vp) and all(v > 0 for v in vs):
            ratios = [p / s for p, s in zip(vp, vs) if s > 0]
            if ratios:
                vpvs = round(sum(ratios) / len(ratios), 3)
        return z, vp, vpvs

    def _derive_grid_from_velest(self, source_dir=None) -> bool:
        """Override z_nodes/vp_vals (and vpvs) from the VELEST output model so
        the tomography depth layers match the inverted 1D velocity structure.
        Returns True if the model was applied."""
        vel = self._find_velest_outputs(source_dir)
        if not vel["mod"]:
            print("[TOMO] z_from_velest: velest.mod not found — keeping configured Z grid")
            return False
        parsed = self._parse_velest_mod(vel["mod"])
        if not parsed:
            print(f"[TOMO] z_from_velest: could not parse {vel['mod']} — keeping Z grid")
            return False
        z, vp, vpvs = parsed
        self.grid["z_nodes"] = z
        self.grid["vp_vals"] = vp
        if vpvs:
            self.grid["vpvs"] = vpvs
        print(f"[TOMO] Z grid from VELEST ({os.path.basename(vel['mod'])}): "
              f"{len(z)} layers {z[0]:.0f}..{z[-1]:.0f} km, "
              f"Vp {min(vp):.2f}-{max(vp):.2f}, Vp/Vs={self.grid['vpvs']}")
        return True

    # ── Locate the simul2000 binary ───────────────────────────────────────────
    def _find_simul2000(self) -> str:
        home = os.path.expanduser("~")
        candidates = [
            self.tcfg.get("exec", ""),
            os.path.join(self.base_dir, "core", "bin", "simul2000"),
            os.path.join(home, "bin", "simul2000"),
            os.path.join(self.simulflow_src, "simulflow", "bin",
                         "simul2000", "simul2000"),
            os.path.join(self.base_dir, "core", "src", "simul2000", "simul2000"),
            shutil.which("simul2000") or "",
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c
        return ""

    # ── Locate VELEST step outputs (model / stations / events) ────────────────
    def _find_velest_outputs(self, source_dir=None):
        # Explicit dir (e.g. a prior VELEST pipeline job for this config) wins.
        search = [source_dir, self.tcfg.get("velest_dir")]
        search += [
            os.path.join(self.base_dir, "work", "velocity"),
            os.path.join(self.base_dir, "work"),
        ]
        out = {"mod": "", "sta": "", "cnv": ""}
        names = {"mod": ["velest.mod"], "sta": ["velest.sta"],
                 "cnv": ["final.CNV", "final.cnv"]}
        for d in search:
            if not d or not os.path.isdir(d):
                continue
            for key, opts in names.items():
                if out[key]:
                    continue
                for n in opts:
                    p = os.path.join(d, n)
                    if os.path.isfile(p):
                        out[key] = p
        return out

    # ── Locate HypoDD relocation outputs ──────────────────────────────────────
    def _find_hypodd_outputs(self, source_dir=None):
        search = [source_dir, self.tcfg.get("hypodd_dir")]
        search += glob.glob(os.path.join(self.base_dir, "work", "relocation*"))
        search += [os.path.join(self.base_dir, "work", "relocation"),
                   os.path.join(self.base_dir, "work")]
        out = {"reloc": "", "phase": ""}
        for d in search:
            if not d or not os.path.isdir(d):
                continue
            if not out["reloc"]:
                p = os.path.join(d, "hypoDD.reloc")
                if os.path.isfile(p):
                    out["reloc"] = p
            if not out["phase"]:
                # "phase.dat" is what HypoDD's own `catalog` mode writes
                # (_write_phase_dat in hypodd.py): same header+pick-line
                # format Hypodd2simul2000 parses, just a different filename
                # than the VELEST-derived .pha variants.
                for n in ("velesttohypo.pha", "phase.pha", "hypodd_phase.pha", "phase.dat"):
                    p = os.path.join(d, n)
                    if os.path.isfile(p):
                        out["phase"] = p
                        break
        return out

    # ── Build initial 3D MOD + STNS + EQKS + CNTL ─────────────────────────────
    def _prepare_inputs(self, eqks_source: str, source_dir=None) -> int:
        from simulflow.preparation.velest2simul2000 import Velest2simul2000

        # Z layers + initial Vp from the VELEST inverted model (default on).
        # X/Y stay as the configured lateral grid; only depth follows VELEST.
        if bool(self.tcfg.get("z_from_velest", True)):
            self._derive_grid_from_velest(source_dir)

        conv = Velest2simul2000(
            ref_lat=self.ref["lat"], ref_lon=self.ref["lon"],
            ref_elev=self.ref["elev"],
            x_nodes=self.grid["x_nodes"], y_nodes=self.grid["y_nodes"],
            z_nodes=self.grid["z_nodes"], vp_vals=self.grid["vp_vals"],
            vpvs=self.grid["vpvs"], bld=self.grid["bld"],
        )

        stns = os.path.join(self.work_dir, "STNS")
        eqks = os.path.join(self.work_dir, "EQKS")
        mod = os.path.join(self.work_dir, "MOD")
        cntl = os.path.join(self.work_dir, "CNTL")

        vel = self._find_velest_outputs(source_dir)

        # 1. Stations, from velest.sta
        if vel["sta"]:
            conv.convert_stations(vel["sta"], stns)
        else:
            print("[TOMO] WARNING: velest.sta not found — STNS missing")

        # 2. Events (EQKS)
        if eqks_source == "hypodd":
            from simulflow.preparation.hypodd2simul2000 import Hypodd2simul2000
            hyp = self._find_hypodd_outputs(source_dir)
            if not (hyp["reloc"] and hyp["phase"]):
                print("[TOMO] HypoDD outputs incomplete "
                      f"(reloc={bool(hyp['reloc'])}, phase={bool(hyp['phase'])}) "
                      "— falling back to VELEST CNV")
                eqks_source = "velest"
            else:
                h2s = Hypodd2simul2000(work_dir=os.path.dirname(hyp["reloc"]))
                h2s.convert_to_eqks(
                    reloc_file=os.path.basename(hyp["reloc"]),
                    phase_file=os.path.basename(hyp["phase"]),
                    output_eqks="EQKS", calculate_sp=True)
                src_eqks = os.path.join(os.path.dirname(hyp["reloc"]), "EQKS")
                if os.path.abspath(src_eqks) != os.path.abspath(eqks):
                    shutil.copy2(src_eqks, eqks)

        if eqks_source == "velest":
            if not vel["cnv"]:
                print("[ERROR] No final.CNV found — run VELEST step first.")
                return 0
            conv.convert_cnv_to_eqks(vel["cnv"], eqks)

        # 3. Initial 3D grid model
        conv.create_simul_mod(mod)

        # 4. Control file (neqs auto-counted from EQKS)
        neqs = conv.get_initial_neqs(eqks)
        if neqs == 0:
            print("[ERROR] EQKS has 0 events — aborting tomography.")
            return 0
        conv.create_cntl(
            filename=cntl, eqks_file=eqks,
            damp_p=self.damp["damp_p"], damp_s=self.damp["damp_s"],
            hitct=self.damp["hitct"], neqs=neqs)
        return neqs

    # ── Import the SimulFlow plotter ──────────────────────────────────────────
    @staticmethod
    def _import_plotter():
        """Import Simul2000Plotter from simulflow. The 3D interactive tomogram
        (formerly via pyvista) is now handled entirely by the Plotly-based
        Imaging 3D viewer in the web GUI, pyvista is no longer needed."""
        from simulflow.visualization.simul2000_plotter import Simul2000Plotter
        return Simul2000Plotter

    # ── Generate figures from SIMUL2000 outputs ───────────────────────────────
    def _make_plots(self, checkerboard: bool):
        Simul2000Plotter = self._import_plotter()
        plotter = Simul2000Plotter(work_dir=self.work_dir)
        target = int(self.tcfg.get("target_layer", 6))
        z_nodes = list(self.grid.get("z_nodes") or [])
        produced = []

        def _try(label, fn):
            try:
                fn()
                produced.append(label)
            except Exception as e:           # one bad plot must not kill the run
                print(f"[TOMO] plot '{label}' failed: {e}")

        # 2D: grid parameterization (XYZ on map), Vp tomogram, DWS, ray tracing
        _try("parameterization", lambda: plotter.plot_parameterization(
            mod_file="MOD", stns_file="STNS", eqks_file="EQKS",
            show_earthquakes=True))

        # Vp tomogram + DWS: one PNG per depth layer (not just a single
        # target_layer) so the figure gallery shows the whole 3D model, the
        # same set of layers exposed by the Imaging 3D viewer's checklist.
        if z_nodes:
            for iz, depth in enumerate(z_nodes):
                _try(f"velocity_z{iz:02d}", lambda iz=iz: plotter.plot_velocity(
                    model_init="MOD", model_final="velomod.out",
                    target_layer=iz, show_earthquakes=True, eq_files="EQKS",
                    figname=f"Tomo_Results_Topo_z{iz:02d}_{z_nodes[iz]:.0f}km.png"))
                _try(f"dws_z{iz:02d}", lambda iz=iz: plotter.plot_dws(
                    model_awal="MOD", dws_file="output", target_layer=iz,
                    figname=f"DWS_Results_Grayscale_z{iz:02d}_{z_nodes[iz]:.0f}km.png"))
        else:   # grid depths unavailable, fall back to the single target_layer
            _try("velocity", lambda: plotter.plot_velocity(
                model_init="MOD", model_final="velomod.out",
                target_layer=target, show_earthquakes=True, eq_files="EQKS"))
            _try("dws", lambda: plotter.plot_dws(
                model_awal="MOD", dws_file="output", target_layer=target))

        _try("raypaths", lambda: plotter.plot_curved_raypaths(
            model_awal="MOD", stns_file="STNS", eq_file="finalsmpout"))
        if checkerboard:
            _try("checkerboard", lambda: plotter.plot_checkerboard(
                model_init="MOD", model_final="velomod.out",
                target_layer=target))

        return produced

    # ── Run a checkerboard resolution test (synthetic ±anomaly) ───────────────
    def _run_checkerboard(self, neqs: int):
        from simulflow.preparation.simulcheckerboard import SimulCheckerboard
        pct = float(self.tcfg.get("checker_percent", 5.0))
        chk_dir = os.path.join(self.tomo_dir, "checkerboard")
        os.makedirs(chk_dir, exist_ok=True)
        checker = SimulCheckerboard(work_dir=chk_dir)
        checker.build_checkerboard_model(
            x_nodes=self.grid["x_nodes"], y_nodes=self.grid["y_nodes"],
            z_nodes=self.grid["z_nodes"], base_vp=self.grid["vp_vals"],
            anomaly_percent=pct)
        print(f"[TOMO] Checkerboard model (±{pct:.0f}%) → {chk_dir}")

    # ── Run SIMUL2000 ─────────────────────────────────────────────────────────
    def _run_simul(self) -> bool:
        from simulflow.utils.simul2000_run import SimulRun
        runner = SimulRun()
        # Honour an installed/compiled binary over the SimulFlow-bundled one.
        exe = self._find_simul2000()
        if exe:
            runner.exe_path = exe
        if not getattr(runner, "exe_path", "") or not os.path.isfile(runner.exe_path):
            print("[ERROR] simul2000 binary not found — run install.sh STEP 3.")
            return False
        print(f"[TOMO] simul2000 binary: {runner.exe_path}")
        return runner.run(work_dir=self.work_dir, cntl_filename="CNTL")

    # ── Public entry point ────────────────────────────────────────────────────
    def run(self, source_dir=None, eqks_source=None, checkerboard=None):
        """
        source_dir   : dir holding VELEST/HypoDD outputs (else auto-discovered)
        eqks_source  : 'velest' (final.CNV) | 'hypodd' (hypoDD.reloc)
        checkerboard : also build a checkerboard resolution test
        """
        eqks_source = (eqks_source or self.tcfg.get("eqks_source", "hypodd")).lower()
        if checkerboard is None:
            checkerboard = bool(self.tcfg.get("checkerboard", False))

        print(f"[TOMO] SIMUL2000 tomography (events={eqks_source}, "
              f"grid={len(self.grid['x_nodes'])}x{len(self.grid['y_nodes'])}"
              f"x{len(self.grid['z_nodes'])}) ...")
        t0 = time.time()

        neqs = self._prepare_inputs(eqks_source, source_dir)
        if neqs == 0:
            sys.exit(1)
        print(f"[TOMO] neqs = {neqs}")

        ok = self._run_simul()
        if not ok:
            print("[TOMO] WARNING: SIMUL2000 returned non-zero — "
                  "check CNTL/MOD/EQKS in " + self.work_dir)

        if checkerboard:
            try:
                self._run_checkerboard(neqs)
            except Exception as e:
                print(f"[TOMO] checkerboard prep failed: {e}")

        figs = self._make_plots(checkerboard)
        print(f"[TOMO] figures: {', '.join(figs) if figs else 'none'}")

        for fn, label in [("velomod.out", "final 3D model"),
                          ("output", "DWS / run log"),
                          ("finalsmpout", "relocated events")]:
            p = os.path.join(self.work_dir, fn)
            if os.path.exists(p):
                print(f"[TOMO] output ({label}): {p}")

        print(f"[TOMO] Done. ({time.time() - t0:.1f}s)  Results: {self.work_dir}")

#!/usr/bin/env python3
"""
SeisWork - Match&Locate (M&L) template-matching detection module
Author : HakimBMKG

Match&Locate (Zhang & Wen, GJI 2015) is the template-matching detector that
closes the LOC-FLOW workflow (Zhang et al., 2022): catalog events are used
as templates to scan continuous data and detect + locate extra, usually
smaller, events missed by the standard pick->associate->locate chain.
Detections can then be relocated relatively with GrowClust.

Steps reproduced here (MatchLocate/run_matchlocate.sh):
  1. select templates from the catalog (mag >= template_min_mag, first N)
  2. cut + TauP-mark P/S template waveforms (marktaup.py)          -> Template/
  3. run MatchLocate2 per template over each day's continuous data -> <ymd>/
  4. SelectFinal: merge detections across templates, dedup in time -> DetectedFinal.dat
  5. (optional) GrowClust relative relocation of the detections

MatchLocate2's internal SAC bandpass (bpcc -> xapiir, order=4, passes=2) is
reproduced in ObsPy before running (detrend + taper + bandpass corners=4
zerophase=True), then MatchLocate2 runs with -B-1/-1 (no internal filtering) -
same approach verified bit-exact for FDTCC, avoiding the SAC signal-processing
library the local MatchLocate2 build lacks (it links an xapiir guard stub).

Templates are cut from the same pre-filtered continuous SAC that gets
scanned, so template and trace share filtering, sampling and reference time
exactly - this keeps self-detection close to 1.0.

References:
  Zhang, M., and L. Wen (2015), GJI, doi:10.1093/gji/ggu466     (Match&Locate)
  Zhang, M., et al. (2022), SRL, doi:10.1785/0220220019         (LOC-FLOW)
"""

import os
import math
import shutil
import subprocess
import time

import numpy as np
import pandas as pd

from seiswork.modules.relocation.hypodd import CATALOG_COLS


class MatchLocateDetection:
    """Template-matching detection + location with MatchLocate2 / SelectFinal."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = base_dir
        self.dcfg     = cfg.get("detection", {}).get("matchlocate", {})
        self.data     = cfg.get("data", {})

        self.ml_exec     = self._find_exec(self.dcfg.get("exec_ml", "MatchLocate2"),
                                           "MatchLocate2")
        self.select_exec = self._find_exec(self.dcfg.get("exec_select", "SelectFinal"),
                                           "SelectFinal")

        # SDS archive root: explicit data_source.sds_path, else data.waveform_dir
        ds = cfg.get("data_source", {}) if isinstance(cfg.get("data_source"), dict) else {}
        self.sds_path = (ds.get("sds_path") or
                         os.path.join(base_dir, self.data.get("waveform_dir", "")))
        self.station_file = os.path.join(base_dir, self.data.get("station_file", ""))

        # Output tree (mirrors LOC-FLOW MatchLocate/ layout).
        self.out_dir  = os.path.join(base_dir, "work", "detection", "matchlocate")
        self.data_dir = os.path.join(self.out_dir, "Data")       # continuous SAC per day
        self.tmpl_dir = os.path.join(self.out_dir, "Template")   # template SAC + INPUT
        self.log_dir  = os.path.join(base_dir, "work", "logs", "matchlocate")
        for d in (self.out_dir, self.data_dir, self.tmpl_dir,
                  os.path.join(self.tmpl_dir, "INPUT"), self.log_dir):
            os.makedirs(d, exist_ok=True)

        # ── Detection/search parameters (defaults = LOC-FLOW PROC_MatchLocate) ──
        self.template_min_mag = float(self.dcfg.get("template_min_mag", 0.5))
        self.max_templates    = int(self.dcfg.get("max_templates", 10))
        self.comp             = str(self.dcfg.get("comp", "HHZ"))     # vertical channel
        self.dist_max_deg     = float(self.dcfg.get("dist_max_deg", 0.2))
        self.tleng            = float(self.dcfg.get("tleng_sec", 40.0))  # 100 km / 2.5 km/s
        self.both_ps          = int(self.dcfg.get("both_ps", 0))      # 0 = S template only
        self.phase0           = str(self.dcfg.get("phase0", "S"))
        self.multiphase       = int(self.dcfg.get("multiphase", 0))
        # bandpass: reuse hypoDD crosscorr pre_filt band so filtering matches FDTCC.
        pf = (cfg.get("relocation", {}).get("hypodd", {})
              .get("crosscorr", {}).get("pre_filt", [2.0, 2.0, 8.0, 8.0]))
        self.bp_low  = float(self.dcfg.get("bp_low",  pf[1]))
        self.bp_high = float(self.dcfg.get("bp_high", pf[-2]))
        # MatchLocate2 grid / window / thresholds
        self.search_R = str(self.dcfg.get("search_R", "0.0/0.0/0"))   # maxlat/maxlon/maxh
        self.search_I = str(self.dcfg.get("search_I", "0.01/0.01/0.5"))
        self.T_window = str(self.dcfg.get("T_window", "2.0/0.5/1.5"))  # win/before/after
        self.H_thresh = str(self.dcfg.get("H_thresh", "0.0/7.0"))      # CC/N(*MAD)
        self.D_intd   = float(self.dcfg.get("D_intd", 3.0))            # keep 1 ev / D sec
        self.min_stations = int(self.dcfg.get("min_stations", 3))
        # MatchLocate2's Checkdata() requires identical sampling/begin time across
        # all traces; the 7G archive mixes 100/200 Hz, so every trace is
        # interpolated onto one common day grid at target_sr.
        self.target_sr = float(self.dcfg.get("target_sr", 100.0))

        self._taup = None
        self._stadf = None

    # ── binary discovery ──────────────────────────────────────────────────────
    def _find_exec(self, cfg_name: str, fallback: str) -> str:
        found = shutil.which(cfg_name) or shutil.which(fallback)
        if not found:
            for c in (os.path.join(os.path.expanduser("~"), "bin", fallback),
                      os.path.join(self.base_dir, "core", "bin", fallback)):
                if os.path.exists(c):
                    return c
        return found or ""

    # ── velocity model + TauP (reuse FDTCC's resolution) ──────────────────────
    def _build_taup(self):
        if self._taup is not None:
            return self._taup
        from seiswork.modules.relocation.fdtcc import FDTCCDiffTimes
        from obspy.taup.taup_create import build_taup_model
        from obspy.taup import TauPyModel
        tt_dir = os.path.join(self.out_dir, "tt_db")
        os.makedirs(tt_dir, exist_ok=True)
        tops, vp, vs = FDTCCDiffTimes(self.cfg, self.base_dir)._model_layers()
        nd_file = os.path.join(tt_dir, "mymodel.nd")
        with open(nd_file, "w") as f:
            for h, p, s in zip(tops, vp, vs):
                f.write(f"{h:.2f} {p:.3f} {s:.3f} 2.7\n")
            f.write("mantle\n")
            f.write(f"{tops[-1] + 0.1:.2f} {vp[-1]:.3f} {vs[-1]:.3f} 2.7\n")
        npz = os.path.join(tt_dir, "mymodel.npz")
        if not os.path.exists(npz):
            build_taup_model(nd_file, output_folder=tt_dir)
        self._taup = TauPyModel(model=npz)
        return self._taup

    def _stations(self):
        if self._stadf is None:
            from seiswork.utils.converter import _load_station_df
            self._stadf = _load_station_df(self.station_file)
        return self._stadf

    # ── template selection from catalog ───────────────────────────────────────
    def _select_templates(self, catalog_file: str):
        cat = pd.read_csv(catalog_file)
        from obspy import UTCDateTime
        rows = []
        for _, ev in cat.iterrows():
            try:
                mag = float(ev.get("mag", 0.0))
                if np.isnan(mag) or mag < self.template_min_mag:
                    continue
                t = pd.Timestamp(ev["datetime"])
                ot = UTCDateTime(t.year, t.month, t.day, t.hour, t.minute,
                                 t.second + t.microsecond / 1e6)
                rows.append({
                    "otime": ot,
                    "lat": float(ev["lat"]), "lon": float(ev["lon"]),
                    "dep": float(ev.get("depth_km", 10.0)), "mag": mag,
                    "name": f"{ot.year:04d}{ot.month:02d}{ot.day:02d}"
                            f"{ot.hour:02d}{ot.minute:02d}{ot.second + ot.microsecond/1e6:05.2f}",
                    "ymd":  f"{ot.year:04d}{ot.month:02d}{ot.day:02d}",
                })
            except Exception:
                continue
        # strongest first, then cap (LOC-FLOW keeps the first N rows as templates)
        rows.sort(key=lambda r: r["mag"], reverse=True)
        return rows[:self.max_templates]

    # ── continuous SAC per day (filtered, padded to exact day boundary) ───────
    def _prep_continuous(self, ymd: str):
        """SDS -> filtered SAC, trimmed/padded to exactly [day0, day0+86400] so
        every trace shares the same begin time (b=0), length, and o=0 -
        MatchLocate2's Checkdata() requires identical b/delta and o==0."""
        ddir = os.path.join(self.data_dir, ymd)
        if os.path.isdir(ddir) and os.listdir(ddir):
            return ddir                                   # already prepared
        os.makedirs(ddir, exist_ok=True)
        from obspy import UTCDateTime
        from obspy.clients.filesystem.sds import Client as SDSClient

        y, m, d = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
        day0 = UTCDateTime(y, m, d)
        npts = int(round(86400 * self.target_sr)) + 1
        stadf = self._stations()
        try:
            client = SDSClient(self.sds_path)
        except Exception as e:
            print(f"[M&L] Cannot open SDS at {self.sds_path}: {e}")
            return ddir
        n = 0
        for _, r in stadf.iterrows():
            net = str(r.get("network", r.get("net", "7G")))
            sta = str(r["station"])
            try:
                st = client.get_waveforms(net, sta, "*", self.comp, day0, day0 + 86400)
            except Exception:
                st = None
            if not st or len(st) == 0:
                continue
            st.merge(method=1, fill_value=0)
            tr = st[0]
            tr.detrend("constant"); tr.detrend("linear")
            tr.taper(max_percentage=0.01, type="hann")
            tr.filter("bandpass", freqmin=self.bp_low, freqmax=self.bp_high,
                      corners=4, zerophase=True)
            # Pad generously, then resample onto the common day grid
            # (start = day0 exactly, npts identical) to unify sr/begin time.
            try:
                tr.trim(day0 - 5, day0 + 86400 + 5, pad=True, fill_value=0)
                tr.interpolate(sampling_rate=self.target_sr, starttime=day0, npts=npts)
            except Exception as e:
                print(f"[M&L]   {net}.{sta}: resample to day grid failed ({e}); skipped.")
                continue
            out = os.path.join(ddir, f"{net}.{sta}.{self.comp}")
            tr.write(out, format="SAC")
            self._set_sac_dayref(out, day0)
            n += 1
        print(f"[M&L] {ymd}: prepared {n} filtered continuous SAC files "
              f"@ {self.target_sr:g} Hz (uniform day grid).")
        return ddir

    @staticmethod
    def _set_sac_dayref(sac_file: str, day0):
        """Reference time = start of day, b=0, o=0 (data already starts at day0)."""
        from obspy import read
        tr = read(sac_file, format="SAC")[0]
        s = tr.stats.sac
        s["nzyear"], s["nzjday"] = day0.year, day0.julday
        s["nzhour"] = s["nzmin"] = s["nzsec"] = s["nzmsec"] = 0
        s["b"] = 0.0
        s["o"] = 0.0
        tr.write(sac_file, format="SAC")

    # ── template: cut from filtered continuous SAC + TauP-mark P/S ────────────
    def _prep_template(self, ev: dict):
        """Cut the template window from the day's continuous SAC and mark
        theoretical P/S arrivals (marktaup.py equivalent). Writes
        Template/<name>/NET.STA.CHA and Template/INPUT/<name>."""
        from obspy import read, UTCDateTime
        from obspy.geodetics import locations2degrees

        ddir = os.path.join(self.data_dir, ev["ymd"])
        tdir = os.path.join(self.tmpl_dir, ev["name"])
        if os.path.isdir(tdir):
            shutil.rmtree(tdir)
        os.makedirs(tdir, exist_ok=True)
        inp_path = os.path.join(self.tmpl_dir, "INPUT", ev["name"])

        model = self._build_taup()
        day0  = UTCDateTime(int(ev["ymd"][:4]), int(ev["ymd"][4:6]), int(ev["ymd"][6:8]))
        tb    = ev["otime"]
        te    = tb + self.tleng
        stadf = self._stations()
        coords = {str(r["station"]): (float(r["lat"]), float(r["lon"]))
                  for _, r in stadf.iterrows()}

        n_ok = 0
        with open(inp_path, "w") as p:
            for f in sorted(os.listdir(ddir)):
                fp = os.path.join(ddir, f)
                parts = f.split(".")
                if len(parts) < 3:
                    continue
                net, sta, cha = parts[0], parts[1], parts[2]
                if cha[-1] != "Z" or sta not in coords:
                    continue
                stla, stlo = coords[sta]
                dist = locations2degrees(ev["lat"], ev["lon"], stla, stlo)
                if dist >= self.dist_max_deg:
                    continue
                arr = model.get_travel_times(source_depth_in_km=max(ev["dep"], 0.0),
                                             distance_in_degree=dist,
                                             phase_list=["P", "p", "S", "s"])
                p_t = s_t = None
                p_rp = p_hs = s_rp = s_hs = None
                for a in arr:
                    if a.name in ("P", "p") and p_t is None:
                        p_t  = a.time
                        p_rp = a.ray_param * 2 * math.pi / 360
                        p_hs = -1 * (p_rp / 111.19) / math.tan(math.radians(a.takeoff_angle))
                    if a.name in ("S", "s") and s_t is None:
                        s_t  = a.time
                        s_rp = a.ray_param * 2 * math.pi / 360
                        s_hs = -1 * (s_rp / 111.19) / math.tan(math.radians(a.takeoff_angle))
                if s_t is None:
                    continue
                try:
                    tr = read(fp, format="SAC", starttime=tb, endtime=te)[0]
                except Exception:
                    continue
                if tr.stats.npts == 0 or float(np.max(np.abs(tr.data))) == 0:
                    continue
                # SAC reference time = origin (nz=tb, o=0). The cut starts at the
                # first day-grid sample at/after tb, so b carries the sub-sample
                # offset while t1/t2 stay equal to the pure P/S travel times.
                s = tr.stats.sac
                s["nzyear"], s["nzjday"] = tb.year, tb.julday
                s["nzhour"], s["nzmin"]  = tb.hour, tb.minute
                s["nzsec"]  = tb.second
                s["nzmsec"] = int(round(tb.microsecond / 1000.0))
                s["b"] = float(tr.stats.starttime - tb)
                s["o"] = 0.0
                s["stla"], s["stlo"] = stla, stlo
                s["evla"], s["evlo"], s["evdp"] = ev["lat"], ev["lon"], ev["dep"]
                s["user0"] = ev["mag"]
                if p_t is not None:
                    s["t1"] = round(p_t, 2)
                    if self.both_ps == 1:
                        p.write(f"{net}.{sta}.{cha} {p_t:5.2f} "
                                f"{p_rp:.6e}/{p_hs:.6e} 1 P\n")
                s["t2"] = round(s_t, 2)
                p.write(f"{net}.{sta}.{cha} {s_t:5.2f} {s_rp:.6e}/{s_hs:.6e} 2 S\n")
                tr.write(os.path.join(tdir, f"{net}.{sta}.{cha}"), format="SAC")
                n_ok += 1
        return n_ok

    # ── run MatchLocate2 for one (template, day) ──────────────────────────────
    def _run_one(self, ev: dict, ymd: str, run_dir: str) -> str:
        """Build INPUT.in (stations present in both template and continuous
        dirs), run MatchLocate2, return the EventCase.out path (or "")."""
        tdir = os.path.join(self.tmpl_dir, ev["name"])
        ddir = os.path.join(self.data_dir, ymd)
        inp_src = os.path.join(self.tmpl_dir, "INPUT", ev["name"])
        if not (os.path.isdir(tdir) and os.path.isdir(ddir) and os.path.exists(inp_src)):
            return ""

        lines = []
        for ln in open(inp_src):
            sp = ln.split()
            if len(sp) < 5:
                continue
            station, _t1, DT, ttmark, phase = sp[0], sp[1], sp[2], sp[3], sp[4]
            if self.multiphase == 0 and phase != self.phase0:
                continue
            if (os.path.exists(os.path.join(tdir, station)) and
                    os.path.exists(os.path.join(ddir, station))):
                lines.append(f"{os.path.join(tdir, station)} "
                             f"{os.path.join(ddir, station)} {DT} {ttmark} {phase}")
        if len(lines) < 1:
            return ""

        os.makedirs(run_dir, exist_ok=True)
        input_in = os.path.join(run_dir, "INPUT.in")
        with open(input_in, "w") as f:
            f.write(f"{len(lines)}\n")
            f.write("\n".join(lines) + "\n")

        F = f"{ev['lat']:.4f}/{ev['lon']:.4f}/{ev['dep']:.2f}"
        cmd = [self.ml_exec, f"-F{F}", f"-R{self.search_R}", f"-I{self.search_I}",
               f"-T{self.T_window}", f"-H{self.H_thresh}", f"-D{self.D_intd}",
               "-B-1/-1", "-O0", "INPUT.in"]
        log = os.path.join(run_dir, "ml.log")
        with open(log, "w") as lf:
            subprocess.run(cmd, cwd=run_dir, stdout=lf, stderr=subprocess.STDOUT)
        evout = os.path.join(run_dir, "EventCase.out")
        return evout if os.path.exists(evout) else ""

    # ── SelectFinal across templates for one day ──────────────────────────────
    def _select_final(self, ymd: str, det_files: list) -> list:
        """Merge per-template EventCase.out detections (SelectFinal.pl), dedup
        in time with the SelectFinal binary. Returns detection dicts, each
        keeping the detecting template's name as ``ref`` so GrowClust refine
        can attach that template's theoretical P/S phases (gen_input_matchlocate.pl)."""
        if not det_files:
            return []
        sel_dir = os.path.join(self.out_dir, "MultipleTemplate")
        os.makedirs(sel_dir, exist_ok=True)
        allev = os.path.join(sel_dir, f"Allevents_{ymd}")
        mintrace = 0
        with open(allev, "w") as g:
            for f, template in det_files:
                with open(f) as fh:
                    rows = fh.readlines()
                for ln in rows[1:]:                       # skip header
                    sp = ln.split()
                    if len(sp) < 9:
                        continue
                    _no, t, lat, lon, dep, mag, coef, mad, ntr = sp[:9]
                    if int(float(ntr)) > mintrace:
                        # SelectFinal.c reads "%lf*7 %d %s" -> 9 cols incl. ntrace
                        # before the template name. The name has a dot
                        # (YYYYMMDDhhmmss.ss); omitting ntrace would make %d
                        # swallow the integer part and %s grab only the fraction.
                        g.write(f"{t} {lat} {lon} {dep} {mag} {coef} {mad} "
                                f"{int(float(ntr))} {template}\n")

        finalf = allev + ".final"
        if self.select_exec:
            with open(os.path.join(sel_dir, f"select_{ymd}.log"), "w") as lf:
                subprocess.run([self.select_exec, f"-H{self.H_thresh}",
                                f"-D{self.D_intd}", os.path.basename(allev)],
                               cwd=sel_dir, stdout=lf, stderr=subprocess.STDOUT)
        # SelectFinal writes <file>.final
        if not os.path.exists(finalf):
            # no binary or no detections survived; fall back to merged list
            finalf = allev

        y, mo, d = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
        dets = []
        for ln in open(finalf):
            if ln.startswith("#") or not ln.strip():
                continue
            sp = ln.split()
            # SelectFinal .final: No Time Lat Lon Dep Mag Coef N(*MAD) NumTrace Reference (10)
            # Fallback (Allevents):  Time Lat Lon Dep Mag Coef N(*MAD) NumTrace Reference (9)
            try:
                if len(sp) >= 10 and sp[0].isdigit() and finalf.endswith(".final"):
                    _no, tsec, lat, lon, dep, mag, coef, nmad = sp[:8]
                    ref = sp[9]
                else:
                    tsec, lat, lon, dep, mag, coef, nmad = sp[:7]
                    ref = sp[8] if len(sp) > 8 else (sp[7] if len(sp) > 7 else "")
                tsec = float(tsec)
                hh = int(tsec // 3600); mm = int((tsec - hh * 3600) // 60)
                ss = tsec - hh * 3600 - mm * 60
                dets.append({
                    "ymd": ymd, "year": y, "mon": mo, "day": d,
                    "hh": hh, "mm": mm, "ss": ss,
                    "datetime": f"{y:04d}-{mo:02d}-{d:02d}T{hh:02d}:{mm:02d}:{ss:06.3f}",
                    "lat": float(lat), "lon": float(lon),
                    "depth_km": float(dep), "mag": float(mag),
                    "coef": float(coef), "nmad": float(nmad), "ref": ref,
                })
            except Exception:
                continue
        return dets

    @staticmethod
    def _dets_to_catalog(dets: list) -> pd.DataFrame:
        """Detection dicts -> CATALOG_COLS DataFrame."""
        rows = []
        for i, x in enumerate(dets):
            rows.append({
                "event_id": f"ml_{x['ymd']}_{i:04d}",
                "datetime": x["datetime"],
                "lat": x["lat"], "lon": x["lon"],
                "depth_km": x["depth_km"], "mag": x["mag"],
                "rms": float("nan"), "nsta": 0, "gap": float("nan"),
                "method": "matchlocate",
            })
        return pd.DataFrame(rows, columns=CATALOG_COLS)

    # ── public entry point ────────────────────────────────────────────────────
    def run(self, catalog_file: str):
        if not self.ml_exec:
            print("[ERROR] MatchLocate2 binary not found "
                  "(expected in PATH, ~/bin, or core/bin).")
            return None
        print("[M&L] Starting Match&Locate template detection ...")
        t0 = time.time()

        templates = self._select_templates(catalog_file)
        if not templates:
            print(f"[M&L] No catalog events with mag >= {self.template_min_mag}. "
                  "Nothing to do.")
            self._write_outputs(pd.DataFrame(columns=CATALOG_COLS), t0)
            return None
        print(f"[M&L] {len(templates)} template(s) selected "
              f"(mag >= {self.template_min_mag}, max {self.max_templates}).")

        # Days to scan: configurable range, else the templates' own days.
        scan_days = self._scan_days(templates)
        print(f"[M&L] Scanning {len(scan_days)} day(s): {', '.join(scan_days)}")

        # 1) Prepare continuous data for every day touched (template days + scan).
        for ymd in sorted(set(scan_days) | {t["ymd"] for t in templates}):
            self._prep_continuous(ymd)

        # 2) Prepare templates (cut from their own day's filtered continuous SAC).
        ready = []
        for ev in templates:
            n = self._prep_template(ev)
            if n >= 1:
                ready.append(ev)
            print(f"[M&L]   template {ev['name']} (M{ev['mag']:.1f}): {n} trace(s)")
        if not ready:
            print("[M&L] No usable templates (no nearby vertical traces).")
            self._write_outputs(pd.DataFrame(columns=CATALOG_COLS), t0)
            return None

        # 3) Run each template against each scan day, 4) SelectFinal per day.
        all_dets = []
        for ymd in scan_days:
            det_files = []
            for ev in ready:
                run_dir = os.path.join(self.out_dir, "runs", ymd, ev["name"])
                evout = self._run_one(ev, ymd, run_dir)
                if evout:
                    # keep one copy per (day, template) as LOC-FLOW does
                    saved = os.path.join(self.out_dir, ymd, ev["name"])
                    os.makedirs(os.path.dirname(saved), exist_ok=True)
                    shutil.copy(evout, saved)
                    det_files.append((saved, ev["name"]))
            dets = self._select_final(ymd, det_files)
            if dets:
                all_dets.extend(dets)
                print(f"[M&L]   {ymd}: {len(dets)} event(s) detected.")

        catalog = self._dets_to_catalog(all_dets)
        self._write_outputs(catalog, t0)

        # 5) optional GrowClust relative relocation, following LOC-FLOW's
        # gen_input_matchlocate.pl (theoretical-phase hypoDD.pha).
        if self.dcfg.get("growclust_refine", False) and all_dets:
            self._refine_growclust(all_dets)
        return catalog

    def _scan_days(self, templates):
        start = self.dcfg.get("scan_start")
        end   = self.dcfg.get("scan_end")
        if start and end:
            days = pd.date_range(str(start), str(end), freq="D")
            return [d.strftime("%Y%m%d") for d in days]
        return sorted({t["ymd"] for t in templates})

    def _write_outputs(self, catalog: pd.DataFrame, t0: float):
        out_csv = os.path.join(self.out_dir, "catalog_matchlocate.csv")
        catalog.to_csv(out_csv, index=False)
        cdir = os.path.join(self.base_dir, "work", "catalog")
        os.makedirs(cdir, exist_ok=True)
        catalog.to_csv(os.path.join(cdir, "catalog_matchlocate.csv"), index=False)
        print(f"[M&L] Done. {len(catalog)} detection(s) "
              f"→ {out_csv}  ({time.time() - t0:.1f}s)")

    # ── template theoretical phases (from template SAC t1/t2 headers) ──────────
    def _template_phases(self, ref: str):
        """{station_code: (t1_P, t2_S)} read from Template/<ref>/NET.STA.CHA SAC
        headers - the theoretical travel times marked at template prep, reused
        as the detection's picks (gen_input_matchlocate.pl step 3)."""
        from obspy import read
        cache = getattr(self, "_tphase_cache", None)
        if cache is None:
            cache = self._tphase_cache = {}
        if ref in cache:
            return cache[ref]
        tdir = os.path.join(self.tmpl_dir, ref)
        phases = {}
        if os.path.isdir(tdir):
            for f in sorted(os.listdir(tdir)):
                parts = f.split(".")
                if len(parts) < 3 or parts[2][-1] != "Z":
                    continue
                try:
                    s = read(os.path.join(tdir, f), format="SAC", headonly=True)[0].stats.sac
                    t1 = s.get("t1"); t2 = s.get("t2")
                    if t2 is not None:
                        phases[parts[1]] = (t1, t2)
                except Exception:
                    continue
        cache[ref] = phases
        return phases

    # ── GrowClust refinement (LOC-FLOW gen_input_matchlocate.pl) ───────────────
    def _refine_growclust(self, dets: list):
        """Relocate M&L detections relatively with GrowClust, following
        LOC-FLOW's gen_input_matchlocate.pl: build a theoretical-phase
        hypoDD.pha (picks = detecting template's P/S travel times),
        ph2dt -> dt.ct, FDTCC -> real waveform dt.cc, then growclust."""
        from seiswork.modules.relocation.growclust import GrowClustRelocation
        from seiswork.modules.relocation.hypodd import HypoDDRelocation

        print("[M&L] GrowClust refinement (theoretical-phase, LOC-FLOW) ...")
        gc_dir = os.path.join(self.out_dir, "GrowClust")
        in_dir = os.path.join(gc_dir, "IN")
        tt_dir = os.path.join(gc_dir, "TT")
        gout   = os.path.join(gc_dir, "OUT")
        for d in (in_dir, tt_dir, gout):
            os.makedirs(d, exist_ok=True)

        gc = GrowClustRelocation(self.cfg, self.base_dir)
        gc.out_dir = gc_dir
        hd = HypoDDRelocation(self.cfg, self.base_dir)

        # 1) station.dat (hypoDD fmt) for ph2dt
        station_dat = hd._write_station_dat(in_dir)

        # 2) hypoDD.pha (theoretical picks) + evlist.txt (free-format, fmt=1)
        pha = os.path.join(in_dir, "hypoDD.pha")
        evl = os.path.join(in_dir, "evlist.txt")
        nptot = 0
        with open(pha, "w") as ph, open(evl, "w") as el:
            for i, x in enumerate(dets, 1):
                hdr = (f"{x['year']} {x['mon']} {x['day']} {x['hh']} {x['mm']} "
                       f"{x['ss']:.2f} {x['lat']:.4f} {x['lon']:.4f} "
                       f"{x['depth_km']:.2f} {x['mag']:.2f} 0 0 0 {i}")
                ph.write(f"# {hdr}\n")
                el.write(f"{hdr}\n")
                for sta, (t1, t2) in self._template_phases(x["ref"]).items():
                    if t1 is not None:
                        ph.write(f"{sta} {t1:.3f} 1 P\n")
                    ph.write(f"{sta} {t2:.3f} 1 S\n")
                    nptot += 1
        print(f"[M&L]   hypoDD.pha: {len(dets)} events, {nptot} S-picks (theoretical).")
        # phase.dat alias (ph2dt reads phase.dat name via ph2dt.inp anyway)
        shutil.copy(pha, os.path.join(in_dir, "phase.dat"))

        # 3) ph2dt -> dt.ct, event.sel, event.dat
        ph2dt_inp = hd._write_ph2dt_inp(os.path.join(in_dir, "phase.dat"),
                                        station_dat, in_dir)
        hd._run_ph2dt(ph2dt_inp, in_dir)
        dt_ct = os.path.join(in_dir, "dt.ct")
        if not os.path.exists(dt_ct):
            print("[M&L]   ph2dt produced no dt.ct (detections too sparse) — "
                  "GrowClust refine skipped.")
            return

        # 4) stlist + vzmodel (before FDTCC overwrites station.dat with REAL fmt)
        gc._write_stlist(station_dat, in_dir)
        gc._write_vzmodel(in_dir)

        # 5) dt.cc: real FDTCC waveform CC (fallback: catalog dt.ct -> dt.cc)
        used_fdtcc = False
        try:
            from seiswork.modules.relocation.fdtcc import FDTCCDiffTimes
            cc = FDTCCDiffTimes(self.cfg, self.base_dir).compute(in_dir)
            if cc:
                shutil.copy(cc, os.path.join(in_dir, "dt.cc"))
                used_fdtcc = True
        except Exception as e:
            print(f"[M&L]   FDTCC dt.cc unavailable ({e}); using catalog dt.")
        if not used_fdtcc:
            gc._dtct_to_dtcc(dt_ct, in_dir)
        else:
            print("[M&L]   Using real FDTCC waveform-CC dt.cc.")

        # 6) growclust
        inp = gc._write_growclust_inp(in_dir, tt_dir, gout)
        gc._run_growclust(inp)
        cat_file = os.path.join(gout, "out.growclust_cat")
        df = gc._parse_cat(cat_file, "matchlocate_growclust")
        if not df.empty:
            out_csv = os.path.join(self.out_dir, "catalog_matchlocate_growclust.csv")
            df.to_csv(out_csv, index=False)
            cdir = os.path.join(self.base_dir, "work", "catalog")
            os.makedirs(cdir, exist_ok=True)
            df.to_csv(os.path.join(cdir, "catalog_matchlocate_growclust.csv"), index=False)
            print(f"[M&L] GrowClust-refined catalog: {len(df)} events → {out_csv}")
        else:
            print("[M&L] GrowClust produced no relocated events "
                  "(detections likely too sparse to link).")

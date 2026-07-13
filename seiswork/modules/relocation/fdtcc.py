#!/usr/bin/env python3
"""
SeisWork - FDTCC real waveform cross-correlation differential times (dt.cc)
Author : HakimBMKG

FDTCC (Liu & Zhang, 2019/2022) is the fast double-difference cross-correlation
engine used in LOC-FLOW to compute waveform-similarity differential times
(dt.cc) for hypoDD / GrowClust / tomoDD. Replaces SeisWork's earlier
placeholder catalog-dt "dt.cc" with genuine waveform CC.

FDTCC's internal SAC bandpass (bpcc -> xapiir, order=4, passes=2) is
reproduced here in ObsPy before running (detrend + taper + bandpass
corners=4 zerophase=True), then FDTCC runs with -B-1/-1 (no internal
filtering). Verified bit-exact against the FDTCC Demo reference dt.cc, and
avoids vendoring the SAC signal-processing library the local FDTCC source lacks.

FDTCC inputs (LOC-FLOW steps):
  station.dat   REAL format:  lon lat net sta comp elev
  tt_db/ttdb.txt travel-time table (ObsPy TauP), generated here from the model
  waveforms/    SAC, layout YYYYMMDD/NET.STA.CHAN (F=0 continuous), pre-filtered
  event.sel     ph2dt output (already produced by the hypoDD stage)
  dt.ct         ph2dt output (already produced by the hypoDD stage)
  hypoDD.pha    phase file   (already produced by the hypoDD stage)

References:
  Liu, M., et al. (2022), JGR Solid Earth, doi:10.1029/2022JB024091  (FDTCC)
  Zhang, M., et al. (2022), SRL, doi:10.1785/0220220019  (LOC-FLOW)
"""

import os
import math
import shutil
import subprocess

import numpy as np
import pandas as pd


class FDTCCDiffTimes:
    """Prepare inputs and run FDTCC to produce a real waveform-CC dt.cc."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = base_dir
        self.rcfg     = cfg.get("relocation", {}).get("hypodd", {}).get("crosscorr", {})
        self.data     = cfg.get("data", {})

        self.exec = self._find_exec()
        # SDS archive root: explicit data_source.sds_path, else waveform_dir
        ds = cfg.get("data_source", {}) if isinstance(cfg.get("data_source"), dict) else {}
        self.sds_path = (ds.get("sds_path") or
                         os.path.join(base_dir, self.data.get("waveform_dir", "")))
        self.station_file = os.path.join(base_dir, self.data.get("station_file", ""))

        # CC parameters (defaults match the FDTCC Demo / LOC-FLOW gen_input.pl).
        self.bp_low   = float(self.rcfg.get("pre_filt", [2.0, 2.0, 8.0, 8.0])[1])
        self.bp_high  = float(self.rcfg.get("pre_filt", [2.0, 2.0, 8.0, 8.0])[-2])
        self.cc_thr   = float(self.rcfg.get("cc_threshold", 0.7))
        self.snr_thr  = float(self.rcfg.get("snr_threshold", 1.0))
        self.dt_max   = float(self.rcfg.get("dt_max_sec", 2.0))
        # waveform windows: wb/wa/wf (P) , wbs/was/wfs (S)
        self.win      = self.rcfg.get("fdtcc_window", "0.2/1.0/0.3/0.5/1.5/0.5")
        self.grid     = self.rcfg.get("fdtcc_grid",   "3/20/0.02/2")  # trx/trh/tdx/tdh

    def _find_exec(self) -> str:
        name = self.rcfg.get("exec_fdtcc", "FDTCC")
        found = shutil.which(name) or shutil.which("FDTCC")
        if not found:
            for c in [os.path.join(os.path.expanduser("~"), "bin", "FDTCC"),
                      os.path.join(self.base_dir, "core", "bin", "FDTCC")]:
                if os.path.exists(c):
                    return c
        return found or ""

    # ── station.dat  (REAL format: lon lat net sta comp elev) ─────────────────
    def _write_station_dat(self, out_file: str, sta_bands: dict | None = None):
        """sta_bands: {station: band} (e.g. {'SBSSI': 'SH'}) from
        _detect_station_bands() - the comp column must match the channel
        code used for that station's SAC files in _prep_waveforms(), or
        FDTCC won't find the waveform. Falls back to the standard
        HH > BH > EH > SH default band when a station is missing from the map."""
        from seiswork.utils.converter import _load_station_df
        from seiswork.utils.channels import BAND_PRIORITY
        sta_bands = sta_bands or {}
        df = _load_station_df(self.station_file)
        with open(out_file, "w") as f:
            for _, r in df.iterrows():
                net = str(r.get("network", r.get("net", "7G")))
                sta = str(r["station"])
                comp = f"{sta_bands.get(sta, BAND_PRIORITY[0])}Z"
                f.write(f"{float(r['lon']):.5f} {float(r['lat']):.5f} "
                        f"{net} {sta} {comp} {float(r['elev'])/1000.0:.4f}\n")
        return out_file

    # ── per-station band auto-detect (HH > BH > EH > SH) ───────────────────────
    @staticmethod
    def _parse_event_days(event_sel: str) -> set:
        days = set()
        for ln in open(event_sel):
            p = ln.split()
            if len(p) >= 1 and len(p[0]) == 8 and p[0].isdigit():
                days.add(p[0])                      # YYYYMMDD
        return days

    def _detect_station_bands(self, nets: dict, days: set, client) -> dict:
        """Detect each station's available band once by checking the Z
        channel against every event day until one is found - a real network
        mixes broadband and short-period sensors, so this must not assume
        HH everywhere."""
        from obspy import UTCDateTime
        from seiswork.utils.channels import BAND_PRIORITY
        bands = {}
        for sta, net in nets.items():
            chosen = BAND_PRIORITY[0]
            for b in BAND_PRIORITY:
                found = False
                for ymd in sorted(days):
                    day0 = UTCDateTime(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
                    try:
                        st = client.get_waveforms(net, sta, "*", f"{b}Z", day0, day0 + 86400)
                    except Exception:
                        st = None
                    if st:
                        found = True
                        break
                if found:
                    chosen = b
                    break
            bands[sta] = chosen
        return bands

    # ── travel-time table via ObsPy TauP (REAL ttdb.txt format) ───────────────
    def _model_layers(self):
        """(tops, vp, vs) - reuse hypoDD's velocity-model resolution."""
        vel_mod = os.path.join(self.base_dir, "work", "velocity", "velocity_updated.mod")
        if not os.path.exists(vel_mod):
            vel_mod = os.path.join(self.base_dir, "config", "velocity.mod")
        tops, vp = [], []
        if os.path.exists(vel_mod):
            for line in open(vel_mod):
                if line.startswith("*") or not line.strip():
                    continue
                p = line.split()
                if len(p) >= 2:
                    try:
                        tops.append(float(p[0])); vp.append(float(p[1]))
                    except ValueError:
                        pass
        if not tops:
            tops = [0.0, 1.0, 3.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 60.0]
            vp   = [4.70, 5.28, 5.41, 5.41, 5.71, 6.02, 6.54, 7.99, 8.05, 8.10, 8.10]
        vpvs = float(self.cfg.get("relocation", {}).get("hypodd", {})
                     .get("hypodd", {}).get("vpvs", 1.73))
        vs = [v / vpvs for v in vp]
        return tops, vp, vs

    def _build_ttdb(self, tt_dir: str, max_dep: int = 20, max_deg: float = 3.0):
        """Generate tt_db/ttdb.txt: dist dep tp ts tp_rayp ts_rayp tp_hslow
        ts_hslow P S - same recipe as REAL's tt_db/taup_tt.py."""
        os.makedirs(tt_dir, exist_ok=True)
        nd_file  = os.path.join(tt_dir, "mymodel.nd")
        ttdb     = os.path.join(tt_dir, "ttdb.txt")
        tops, vp, vs = self._model_layers()
        with open(nd_file, "w") as f:
            for h, p, s in zip(tops, vp, vs):
                f.write(f"{h:.2f} {p:.3f} {s:.3f} 2.7\n")
            f.write("mantle\n")
            f.write(f"{tops[-1]+0.1:.2f} {vp[-1]:.3f} {vs[-1]:.3f} 2.7\n")

        from obspy.taup.taup_create import build_taup_model
        from obspy.taup import TauPyModel
        build_taup_model(nd_file, output_folder=tt_dir)
        model = TauPyModel(model=os.path.join(tt_dir, "mymodel.npz"))

        with open(ttdb, "w") as f:
            for dep in range(0, max_dep + 1, 2):
                ndist = int(round(max_deg / 0.02))
                for k in range(1, ndist + 1):
                    dist = k * 0.02
                    arr = model.get_travel_times(source_depth_in_km=dep,
                                                 distance_in_degree=dist,
                                                 phase_list=["P", "p", "S", "s"])
                    p_t = s_t = p_rp = s_rp = p_hs = s_hs = None
                    pn = sn = "P"
                    for a in arr:
                        if a.name in ("P", "p") and p_t is None:
                            pn = a.name; p_t = a.time
                            p_rp = a.ray_param * 2 * math.pi / 360
                            p_hs = -1 * (p_rp / 111.19) / math.tan(math.radians(a.takeoff_angle))
                        if a.name in ("S", "s") and s_t is None:
                            sn = a.name; s_t = a.time
                            s_rp = a.ray_param * 2 * math.pi / 360
                            s_hs = -1 * (s_rp / 111.19) / math.tan(math.radians(a.takeoff_angle))
                    if p_t is None or s_t is None:
                        continue
                    f.write(f"{dist} {dep} {p_t} {s_t} {p_rp} {s_rp} "
                            f"{p_hs} {s_hs} {pn} {sn}\n")
        return ttdb

    # ── waveforms: SDS -> filtered SAC, layout YYYYMMDD/NET.STA.CHAN (F=0) ─────
    def _prep_waveforms(self, wav_dir: str, event_sel: str, comps=None, sta_bands=None):
        """For every day spanned by the events, fetch each station's traces
        from the SDS, apply the verified bandpass, and write SAC files whose
        reference time is the start of the day (O = 0 at day start, matching
        FDTCC's F=0 continuous convention).

        comps: explicit 3-letter channel codes to force for every station
        (e.g. ('HHZ','HHN','HHE')).
        sta_bands: {station: band} from _detect_station_bands(), so the
        channel used here matches the comp column written to station.dat.
        When both are None, the band is auto-detected per station (standard
        priority HH > BH > EH > SH) since a real network mixes sensor types.
        """
        import warnings
        from obspy import UTCDateTime
        from obspy.clients.filesystem.sds import Client as SDSClient
        from obspy.io.mseed.headers import InternalMSEEDWarning
        from seiswork.utils.converter import _load_station_df
        from seiswork.utils.channels import BAND_PRIORITY
        warnings.filterwarnings("ignore", category=InternalMSEEDWarning)

        days = self._parse_event_days(event_sel)
        if not days:
            print("[FDTCC] No event days parsed from event.sel.")
            return 0

        stadf = _load_station_df(self.station_file)
        nets  = {str(r["station"]): str(r.get("network", r.get("net", "7G")))
                 for _, r in stadf.iterrows()}

        try:
            client = SDSClient(self.sds_path)
        except Exception as e:
            print(f"[FDTCC] Cannot open SDS at {self.sds_path}: {e}")
            return 0

        # Detect each station's available band once (cached across all days)
        # instead of assuming HH everywhere - many networks mix broadband and
        # short-period sensors.
        sta_band_cache: dict[str, list[str]] = {}

        def _channels_for(sta, net, day0):
            if comps:
                return list(comps)
            if sta_bands and sta in sta_bands:
                return [f"{sta_bands[sta]}{o}" for o in "ZNE"]
            if sta in sta_band_cache:
                return sta_band_cache[sta]
            chosen = [f"{BAND_PRIORITY[0]}{o}" for o in "ZNE"]
            for b in BAND_PRIORITY:
                try:
                    st = client.get_waveforms(net, sta, "*", f"{b}Z", day0, day0 + 86400)
                except Exception:
                    st = None
                if st:
                    chosen = [f"{b}{o}" for o in "ZNE"]
                    break
            sta_band_cache[sta] = chosen
            return chosen

        nwrite = 0
        for ymd in sorted(days):
            y, m, d = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
            day0 = UTCDateTime(y, m, d)
            ddir = os.path.join(wav_dir, ymd)
            os.makedirs(ddir, exist_ok=True)
            for sta, net in nets.items():
                for cha in _channels_for(sta, net, day0):
                    try:
                        st = client.get_waveforms(net, sta, "*", cha,
                                                  day0, day0 + 86400)
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
                    # SAC reference = day start, O = 0 at day start
                    tr.stats.sac = tr.stats.get("sac", {})
                    out = os.path.join(ddir, f"{net}.{sta}.{cha}")
                    tr.write(out, format="SAC")
                    # patch SAC header: nz* = day start, b = offset, o = 0
                    self._set_sac_dayref(out, day0, tr.stats.starttime)
                    nwrite += 1
        print(f"[FDTCC] Prepared {nwrite} filtered SAC files over {len(days)} day(s).")
        return nwrite

    @staticmethod
    def _set_sac_dayref(sac_file: str, day0, tstart):
        """Set SAC reference time to the start of the day and O = 0 there."""
        from obspy import read
        st = read(sac_file, format="SAC")
        tr = st[0]
        tr.stats.sac["nzyear"] = day0.year
        tr.stats.sac["nzjday"] = day0.julday
        tr.stats.sac["nzhour"] = 0
        tr.stats.sac["nzmin"]  = 0
        tr.stats.sac["nzsec"]  = 0
        tr.stats.sac["nzmsec"] = 0
        tr.stats.sac["b"]      = float(tstart - day0)
        tr.stats.sac["o"]      = 0.0
        tr.write(sac_file, format="SAC")

    # ── Run FDTCC -> dt.cc ───────────────────────────────────────────────────
    def compute(self, work_dir: str) -> str:
        """work_dir must already contain event.sel, dt.ct and hypoDD.pha
        (produced by the hypoDD ph2dt stage). Returns the dt.cc path or ""."""
        if not self.exec:
            print("[FDTCC] FDTCC binary not found (build src/FDTCC or set exec_fdtcc).")
            return ""
        for need in ("event.sel", "dt.ct", "hypoDD.pha"):
            if not os.path.exists(os.path.join(work_dir, need)):
                print(f"[FDTCC] Missing required input: {need} in {work_dir}")
                return ""

        # Detect each station's band once and reuse it for both station.dat's
        # comp column and the SAC filenames in waveforms/ - a mismatch means
        # FDTCC can't find the file it's told to look for.
        from seiswork.utils.converter import _load_station_df
        from obspy.clients.filesystem.sds import Client as SDSClient
        stadf = _load_station_df(self.station_file)
        nets  = {str(r["station"]): str(r.get("network", r.get("net", "7G")))
                 for _, r in stadf.iterrows()}
        days  = self._parse_event_days(os.path.join(work_dir, "event.sel"))
        try:
            sta_bands = self._detect_station_bands(nets, days, SDSClient(self.sds_path))
        except Exception:
            sta_bands = {}

        sta_dat = self._write_station_dat(os.path.join(work_dir, "station.dat"), sta_bands=sta_bands)
        tt_dir  = os.path.join(work_dir, "tt_db")
        ttdb    = self._build_ttdb(tt_dir)
        wav_dir = os.path.join(work_dir, "waveforms")
        nwav    = self._prep_waveforms(wav_dir, os.path.join(work_dir, "event.sel"), sta_bands=sta_bands)
        if nwav == 0:
            print("[FDTCC] No waveforms prepared — cannot compute dt.cc.")
            return ""

        # Pre-filtered in ObsPy, so run FDTCC with -B-1/-1 (no internal filter).
        cmd = [self.exec,
               "-F0", "-B-1/-1", "-C1/1/1",
               f"-W{self.win}",
               f"-D0.01/{self.cc_thr}/{self.snr_thr}/{self.dt_max}",
               f"-G{self.grid}",
               sta_dat, ttdb, wav_dir,
               os.path.join(work_dir, "event.sel"),
               os.path.join(work_dir, "dt.ct"),
               os.path.join(work_dir, "hypoDD.pha")]
        log = os.path.join(work_dir, "fdtcc.log")
        print(f"[FDTCC] Running: {' '.join(cmd)}")
        with open(log, "w") as lf:
            subprocess.run(cmd, cwd=work_dir, stdout=lf, stderr=subprocess.STDOUT)
        # FDTCC leaves Input.* scratch files behind
        for junk in ("Input.p", "Input.s1", "Input.s2"):
            jp = os.path.join(work_dir, junk)
            if os.path.exists(jp):
                os.remove(jp)

        dtcc = os.path.join(work_dir, "dt.cc")
        if os.path.exists(dtcc):
            npair = sum(1 for l in open(dtcc) if l.startswith("#"))
            nobs  = sum(1 for l in open(dtcc) if not l.startswith("#") and l.strip())
            print(f"[FDTCC] dt.cc written: {npair} pairs, {nobs} CC observations.")
            return dtcc
        print(f"[FDTCC] dt.cc not produced. Check {log}")
        return ""

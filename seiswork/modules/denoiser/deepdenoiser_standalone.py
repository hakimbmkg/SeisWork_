#!/usr/bin/env python3
"""
SeisWork — DeepDenoiser standalone runner
Author : HakimBMKG

Baca waveform dari SDS/file, terapkan SeisBench DeepDenoiser, simpan ke
output directory dalam format SDS (net/sta/chan.D/...).

Hasil denoised waveform dapat langsung dipakai sebagai input PhaseNet
dengan data_source="sds" / data_source="file".
"""

import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class DeepDenoiserRunner:
    """Standalone DeepDenoiser: baca SDS → denoise → tulis SDS output."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = Path(base_dir)
        dcfg          = cfg.get("denoise", {}).get("deepdenoiser", {})

        self.pretrained  = dcfg.get("pretrained",  "original")
        self.data_source = dcfg.get("data_source",  "sds")
        self.batch_size  = int(dcfg.get("batch_size", 1))
        self.workers     = int(dcfg.get("workers",    4))
        self.highpass    = float(dcfg.get("highpass_hz", 0.0))

        # SDS input path
        wv_dir = self.base_dir / cfg["data"]["waveform_dir"]
        self.sds_path = Path(dcfg.get("sds_path") or str(wv_dir))

        # Output directory (SDS-compatible) — default work/denoised_sds/
        _od = cfg["data"].get("denoised_dir", "")
        self.out_dir = Path(_od) if _od else self.base_dir / "work" / "denoised_sds"
        self.out_dir.mkdir(parents=True, exist_ok=True)

        # Time range from region config
        self.t_start_str = cfg["region"]["starttime"]
        self.t_end_str   = cfg["region"]["endtime"]

    # ── Load DeepDenoiser model ─────────────────────────────────────────────
    def _load_model(self):
        try:
            import seisbench.models as sbm
        except ImportError:
            print("[ERROR] seisbench not installed.", flush=True)
            sys.exit(1)
        import torch

        torch.set_num_threads(1)
        torch.backends.cudnn.benchmark = False

        print(f"[DeepDenoiser] Loading pretrained='{self.pretrained}' ...", flush=True)
        try:
            model = sbm.DeepDenoiser.from_pretrained(self.pretrained)
        except Exception as e:
            print(f"[ERROR] Failed to load DeepDenoiser '{self.pretrained}': {e}", flush=True)
            print("[ERROR] Pastikan ada koneksi internet saat pertama kali download, "
                  "atau copy cache ~/.seisbench dari mesin lain.", flush=True)
            sys.exit(1)

        if torch.cuda.is_available():
            model = model.cuda()
            print(f"[DeepDenoiser] GPU: {torch.cuda.get_device_name(0)}", flush=True)
        else:
            print("[DeepDenoiser] CPU mode", flush=True)
        model.eval()
        return model

    # ── Collect station-day tasks dari SDS ─────────────────────────────────
    def _collect_tasks(self) -> list:
        from obspy import UTCDateTime
        import fnmatch

        t_start = UTCDateTime(self.t_start_str)
        t_end   = UTCDateTime(self.t_end_str)

        network  = self.cfg["data"].get("network", "*")
        cfg_chan  = self.cfg["data"].get("channels", "") or ""
        req_years = set(range(t_start.year, t_end.year + 1))

        _PREF = ["HH?", "EH?", "BH?", "SH?", "EN?", "?H?", "?N?", "*"]

        def _best(avail, pattern):
            if pattern and any(fnmatch.fnmatch(c, pattern) for c in avail):
                return pattern
            for pat in _PREF:
                hits = [c for c in avail if fnmatch.fnmatch(c, pat)]
                if hits:
                    return sorted(hits)[0][:2] + "?"
            return pattern or "?H?"

        sta_chans: dict = {}
        for yr_dir in sorted(self.sds_path.iterdir()):
            if not yr_dir.is_dir() or not yr_dir.name.isdigit():
                continue
            if int(yr_dir.name) not in req_years:
                continue
            net_pat = network if network != "*" else "*"
            for net_dir in sorted(yr_dir.glob(net_pat)):
                if not net_dir.is_dir():
                    continue
                for sta_dir in sorted(net_dir.iterdir()):
                    if not sta_dir.is_dir():
                        continue
                    key = (net_dir.name, sta_dir.name)
                    for ch_dir in sta_dir.iterdir():
                        if ch_dir.is_dir() and ch_dir.name.endswith(".D"):
                            sta_chans.setdefault(key, set()).add(ch_dir.name[:-2])

        tasks = []
        day0 = UTCDateTime(t_start.year, t_start.month, t_start.day)
        for (net, sta), avail in sorted(sta_chans.items()):
            chan = _best(avail, cfg_chan)
            t = day0
            while t < t_end:
                t_next = t + 86400
                if t_next > t_start:
                    tasks.append((net, sta, "*", chan,
                                  float(max(t, t_start)),
                                  float(min(t_next, t_end))))
                t = t_next
        return tasks

    # ── Denoise satu stream + simpan ke output SDS ─────────────────────────
    def _process(self, model, net, sta, chan, t0u, t1u) -> bool:
        import torch
        from obspy.clients.filesystem.sds import Client as SDSClient

        try:
            sds = SDSClient(str(self.sds_path))
            st  = sds.get_waveforms(net, sta, "*", chan, t0u, t1u)
            if len(st) == 0:
                return False
            st.merge(fill_value="interpolate")
            st.detrend("demean")
            if self.highpass > 0:
                st.filter("highpass", freq=self.highpass)
        except Exception as e:
            logger.debug(f"SDS read failed {net}.{sta}: {e}")
            return False

        # Apply DeepDenoiser
        try:
            with torch.no_grad():
                st_ann = model.annotate(st, batch_size=self.batch_size)
        except Exception as e:
            logger.warning(f"[DeepDenoiser] annotate failed {net}.{sta}: {e}")
            return False

        # Ambil hanya trace sinyal (channel berakhir __DeepDenoiser_signal)
        from obspy import Stream
        signal_traces = []
        for tr in st_ann:
            if "__DeepDenoiser_signal" in tr.stats.channel:
                orig_ch = tr.stats.channel.split("__")[0]
                tr_out  = tr.copy()
                tr_out.stats.channel = orig_ch
                signal_traces.append(tr_out)

        if not signal_traces:
            logger.warning(f"[DeepDenoiser] no signal traces for {net}.{sta}")
            return False

        st_clean = Stream(signal_traces)

        # Tulis ke output SDS
        for tr in st_clean:
            tr_net  = tr.stats.network
            tr_sta  = tr.stats.station
            tr_loc  = tr.stats.location or ""
            tr_chan = tr.stats.channel
            day     = t0u

            dir_path = (self.out_dir / str(day.year) / tr_net / tr_sta
                        / f"{tr_chan}.D")
            dir_path.mkdir(parents=True, exist_ok=True)
            fpath = (dir_path /
                     f"{tr_net}.{tr_sta}.{tr_loc}.{tr_chan}.D"
                     f".{day.year}.{day.julday:03d}")
            try:
                tr.write(str(fpath), format="MSEED")
            except Exception as e:
                logger.warning(f"write failed {fpath}: {e}")

        return True

    # ── Public entry ────────────────────────────────────────────────────────
    def run(self):
        from obspy import UTCDateTime

        tasks = self._collect_tasks()
        if not tasks:
            print(f"[DeepDenoiser] Tidak ada data ditemukan di {self.sds_path}", flush=True)
            sys.exit(1)

        print(f"[DeepDenoiser] {len(tasks)} station-day tasks  "
              f"pretrained={self.pretrained}  out={self.out_dir}", flush=True)

        model    = self._load_model()
        t0_all   = time.time()
        ok = err = skip = 0

        for i, (net, sta, loc, chan, t0f, t1f) in enumerate(tasks, 1):
            t0u, t1u = UTCDateTime(t0f), UTCDateTime(t1f)
            tag = f"{net}.{sta}  {t0u.strftime('%Y-%m-%d')}"
            success = self._process(model, net, sta, chan, t0u, t1u)
            if success:
                ok += 1
            else:
                err += 1

            elapsed = time.time() - t0_all
            sys.stdout.write(
                f"\r  [{i}/{len(tasks)}] ok={ok} skip={err}  {elapsed:.0f}s  {tag}"
            )
            sys.stdout.flush()

        print(f"\n[DeepDenoiser] Selesai. ok={ok} gagal={err}  "
              f"output={self.out_dir}  ({time.time()-t0_all:.1f}s)", flush=True)
        return str(self.out_dir)

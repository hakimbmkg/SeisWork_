#!/usr/bin/env python3
"""
SeisWork — Preparation: Download Station & Waveforms
Author : HakimBMKG

Downloads station metadata (StationXML) and waveforms (MiniSEED) from
an FDSN server into a local SDS archive, so the picking pipeline (phasenet)
can run offline in subsequent steps.

Workflow:
  DownloadStation  → config/inventory.xml + config/stations.txt
  DownloadWaveforms → SDS archive (data.waveform_dir) per station-day
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


class DownloadStation:
    """Download station metadata (StationXML + text table) from FDSN.

    Output:
      config/inventory.xml     — full StationXML (for ML magnitude response)
      config/stations.txt      — NET|STA|LAT|LON|ELEV  (for GaMMA, NLLoc)
    """

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = Path(base_dir)
        self.fcfg     = cfg.get("fdsn", {})
        self.reg      = cfg["region"]

        self.out_inv  = self.base_dir / cfg["data"].get("inventory",
                                                         "config/inventory.xml")
        self.out_sta  = self.base_dir / cfg["data"].get("station_file",
                                                         "config/stations.txt")
        self.out_inv.parent.mkdir(parents=True, exist_ok=True)
        self.out_sta.parent.mkdir(parents=True, exist_ok=True)

    def run(self, overwrite: bool = False) -> dict:
        """Download StationXML and create stations.txt.

        Returns dict {inventory, stations_txt, n_stations}.
        """
        from obspy.clients.fdsn import Client as FDSNClient

        if self.out_inv.exists() and not overwrite:
            print(f"[Prep] inventory.xml already exists — skipping "
                  f"(use overwrite=True to re-download)")
            return self._parse_existing()

        client_id = self.fcfg.get("client", "IRIS")
        networks  = ",".join(self.fcfg.get("networks", ["*"]))
        channels  = self.fcfg.get("channels", "HH?,BH?,EH?")
        t0 = self.reg["starttime"]
        t1 = self.reg["endtime"]

        user = self.fcfg.get("user", "")
        pwd  = self.fcfg.get("password", "")
        try:
            if user and pwd:
                client = FDSNClient(client_id, user=user, password=pwd)
            else:
                client = FDSNClient(client_id)
        except Exception as e:
            print(f"[ERROR] FDSN client '{client_id}' failed: {e}")
            sys.exit(1)

        print(f"[Prep] DownloadStation  client={client_id}  net={networks}  "
              f"chan={channels}  {t0} → {t1}")
        try:
            from obspy import UTCDateTime
            inv = client.get_stations(
                network   = networks,
                channel   = channels,
                starttime = UTCDateTime(t0),
                endtime   = UTCDateTime(t1),
                minlatitude  = self.reg.get("lat_min"),
                maxlatitude  = self.reg.get("lat_max"),
                minlongitude = self.reg.get("lon_min"),
                maxlongitude = self.reg.get("lon_max"),
                level        = "response",
            )
        except Exception as e:
            print(f"[ERROR] get_stations failed: {e}")
            sys.exit(1)

        from seiswork.utils.response_fix import fix_inventory_normalization
        for sta, ch, old_a0, new_a0 in fix_inventory_normalization(inv):
            print(f"  [fix] {sta}.{ch}: normalization_factor in FDSN response "
                  f"was {old_a0} — corrected to {new_a0:.6g} (recomputed from "
                  f"poles/zeros; see response_fix.py)")

        inv.write(str(self.out_inv), format="STATIONXML")
        print(f"  inventory.xml → {self.out_inv}  ({self.out_inv.stat().st_size // 1024} KB)")

        rows = []
        for net in inv:
            for sta in net:
                rows.append(f"{net.code}|{sta.code}|{sta.latitude:.6f}|"
                            f"{sta.longitude:.6f}|{sta.elevation:.1f}")
        self.out_sta.write_text("\n".join(rows) + "\n")
        print(f"  stations.txt  → {self.out_sta}  ({len(rows)} stations)")

        return {"inventory": str(self.out_inv),
                "stations_txt": str(self.out_sta),
                "n_stations": len(rows)}

    def _parse_existing(self) -> dict:
        from obspy import read_inventory
        inv   = read_inventory(str(self.out_inv))
        n_sta = sum(len(net) for net in inv)
        return {"inventory": str(self.out_inv),
                "stations_txt": str(self.out_sta),
                "n_stations": n_sta}


class DownloadWaveforms:
    """Download waveforms from FDSN and save to a local SDS archive.

    Uses a per-station-day strategy to avoid OOM.
    Station-days already present in the SDS archive are skipped (unless overwrite=True).

    Output:
      {data.waveform_dir}/ — SDS archive
        YYYY/NET/STA/CHAN.D/NET.STA.LOC.CHAN.D.YYYY.DOY
    """

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = Path(base_dir)
        self.fcfg     = cfg.get("fdsn", {})
        self.reg      = cfg["region"]
        self.pcfg     = cfg.get("pick", {}).get("phasenet", {})

        sds_path = self.pcfg.get("sds_path") or cfg["data"]["waveform_dir"]
        self.sds_path = self.base_dir / sds_path
        self.sds_path.mkdir(parents=True, exist_ok=True)

        self.inv_file = self.base_dir / cfg["data"].get("inventory",
                                                         "config/inventory.xml")
        self.sta_file = self.base_dir / cfg["data"].get("station_file",
                                                         "config/stations.txt")

    def run(self, overwrite: bool = False,
            chunk_days: int = 1, workers: int = 4) -> dict:
        """Download all waveforms to the SDS archive.

        Returns dict {n_downloaded, n_skipped, n_error, sds_path}.
        """
        from obspy import UTCDateTime
        from obspy.clients.fdsn import Client as FDSNClient
        from concurrent.futures import ThreadPoolExecutor, as_completed

        client_id = self.fcfg.get("client", "IRIS")
        channels  = self.fcfg.get("channels", "HH?,BH?,EH?")
        t_start   = UTCDateTime(self.reg["starttime"])
        t_end     = UTCDateTime(self.reg["endtime"])

        user = self.fcfg.get("user", "")
        pwd  = self.fcfg.get("password", "")
        try:
            if user and pwd:
                client = FDSNClient(client_id, user=user, password=pwd)
            else:
                client = FDSNClient(client_id)
        except Exception as e:
            print(f"[ERROR] FDSN client '{client_id}': {e}")
            sys.exit(1)

        stations = self._load_stations()
        if not stations:
            print("[ERROR] No stations found — run DownloadStation first.")
            sys.exit(1)

        tasks = self._build_tasks(stations, channels, t_start, t_end, chunk_days)
        print(f"[Prep] DownloadWaveforms  client={client_id}  "
              f"{len(stations)} stations  {len(tasks)} tasks  workers={workers}")
        print(f"       SDS → {self.sds_path}")

        cnt = {"dl": 0, "skip": 0, "err": 0}
        t_all = time.time()

        def _fetch(task):
            net, sta, loc, chan, t0, t1, fpath = task
            if fpath.exists() and fpath.stat().st_size > 0 and not overwrite:
                return "skip"
            try:
                st = client.get_waveforms(net, sta, loc, chan,
                                          UTCDateTime(t0), UTCDateTime(t1))
                if len(st) == 0:
                    return "err"
                fpath.parent.mkdir(parents=True, exist_ok=True)
                st.write(str(fpath), format="MSEED")
                return "dl"
            except Exception as e:
                logger.debug("download %s.%s %s failed: %s", net, sta,
                             UTCDateTime(t0).strftime("%Y%j"), e)
                return "err"

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_fetch, t): t for t in tasks}
            for fut in as_completed(futs):
                r = fut.result()
                cnt[r] = cnt.get(r, 0) + 1
                done += 1
                if done % 50 == 0 or done == len(tasks):
                    sys.stdout.write(
                        f"\r  [{done}/{len(tasks)}]  dl={cnt['dl']}"
                        f"  skip={cnt['skip']}  err={cnt['err']}"
                        f"  {time.time()-t_all:.0f}s")
                    sys.stdout.flush()
        print(f"\n[Prep] Done — dl={cnt['dl']}  skip={cnt['skip']}"
              f"  err={cnt['err']}  ({(time.time()-t_all)/60:.1f} min)")

        return {"n_downloaded": cnt["dl"], "n_skipped": cnt["skip"],
                "n_error": cnt["err"], "sds_path": str(self.sds_path)}

    def _load_stations(self) -> list:
        """Read stations.txt → list of (net, sta) tuples."""
        if not self.sta_file.exists():
            return []
        try:
            rows = []
            for line in self.sta_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) >= 2:
                    rows.append((parts[0].strip(), parts[1].strip()))
            return sorted(set(rows))
        except Exception as e:
            logger.warning("stations.txt read failed: %s", e)
            return []

    def _build_tasks(self, stations, channels, t_start, t_end, chunk_days) -> list:
        """Build task list per station-channel-day from inventory → SDS paths."""
        from obspy import UTCDateTime, read_inventory

        # Build lookup: (net, sta) → (best_loc, [chan_codes]) from inventory
        sta_channels: dict = {}
        if self.inv_file.exists():
            try:
                inv = read_inventory(str(self.inv_file))
                PREF_ORDER = ["SH", "BH", "HH", "EH"]
                for net in inv:
                    for sta in net:
                        groups: dict = {}
                        for cha in sta:
                            pfx = cha.code[:2]
                            groups.setdefault(pfx, []).append(cha)
                        sel_pfx = next((p for p in PREF_ORDER if p in groups), None)
                        if not sel_pfx and groups:
                            sel_pfx = next(iter(groups))
                        if not sel_pfx:
                            continue
                        locs     = list({c.location_code for c in groups[sel_pfx]})
                        best_loc = "00" if "00" in locs else (locs[0] if locs else "")
                        chan_list = sorted({
                            c.code for c in groups[sel_pfx] if c.location_code == best_loc
                        })
                        sta_channels[(net.code, sta.code)] = (best_loc, chan_list)
            except Exception as e:
                logger.warning("inventory read failed, falling back to wildcard: %s", e)

        tasks = []
        for net, sta in stations:
            if (net, sta) in sta_channels:
                best_loc, chan_list = sta_channels[(net, sta)]
            else:
                # fallback: wildcard when station not in inventory
                best_loc  = ""
                chan_list  = [channels]

            for chan_code in chan_list:
                t = UTCDateTime(t_start.year, t_start.month, t_start.day)
                while t < t_end:
                    day_start = UTCDateTime(t.year, t.month, t.day)
                    t_next    = min(day_start + 86400, t_end)
                    year      = t.year
                    doy       = t.julday
                    fpath = (self.sds_path / str(year) / net / sta
                             / f"{chan_code}.D"
                             / f"{net}.{sta}.{best_loc}.{chan_code}.D.{year}.{doy:03d}")
                    tasks.append((net, sta, best_loc, chan_code,
                                  float(day_start), float(t_next), fpath))
                    t = t_next
        return tasks

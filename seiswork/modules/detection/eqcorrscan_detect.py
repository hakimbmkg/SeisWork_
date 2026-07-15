#!/usr/bin/env python3
"""
SeisWork — EQcorrscan matched-filter detection module (offline)
Author : HakimBMKG

Matched-filter (template-matching) detection with EQcorrscan
(Chamberlain et al., SRL 2018). Catalog events are turned into multi-channel
waveform templates and cross-correlated through continuous SDS data to detect
extra, usually smaller, near-repeating events missed by the standard
pick→associate→locate chain — the offline counterpart of RT-EQcorrscan.

Stages reproduced here mirror the EQcorrscan tutorials
(template_creation.py → matched-filter) and the journal workflow:

  1. Template creation — pick a set of template events from the catalog
     (mag ≥ template_min_mag, strongest first, capped at max_templates), build
     an ObsPy Catalog with their P/S picks (from the sibling picks CSV), cut +
     pre-process (bandpass, resample) the template waveform windows straight
     from the SDS archive → a `Tribe` of `Template`s.
  2. Detection — scan each day of continuous SDS data with the whole Tribe via
     normalised cross-correlation (`Tribe.detect`, FFTW), threshold on MAD (or
     absolute / av_chan_corr) → a `Party` of `Family`(template)→`Detection`s.
  3. Decluster — drop detections closer than `trig_int` seconds (keep the
     highest cross-correlation sum).
  4. (optional) lag-calc — refine per-station pick times by cross-correlating
     each detection against its template (`Party.lag_calc`).

Each detection inherits its template's hypocentre (a matched-filter detection
is, by construction, near-repeating to its template — same source region), so
the output catalog carries the template's lat/lon/depth and, as a first
estimate, its magnitude. `mag_note` records that these are template-inherited.

The whole thing reads continuous data from the SAME SDS archive the rest of
SeisWork uses (obspy.clients.filesystem.sds), so no FDSN/network access is
needed for the offline workflow.

References:
  Chamberlain, C. J., et al. (2018), SRL, doi:10.1785/0220170151  (EQcorrscan)
"""

import os
import time
import glob

import numpy as np
import pandas as pd

from seiswork.modules.relocation.hypodd import CATALOG_COLS


class EQcorrscanDetection:
    """Offline matched-filter detection with EQcorrscan (Tribe → Party)."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = base_dir
        self.dcfg     = cfg.get("detection", {}).get("eqcorrscan", {})
        self.data     = cfg.get("data", {})

        # SDS archive root: explicit data_source.sds_path, else data.waveform_dir
        ds = cfg.get("data_source", {}) if isinstance(cfg.get("data_source"), dict) else {}
        self.sds_path = (ds.get("sds_path") or
                         os.path.join(base_dir, self.data.get("waveform_dir", "")))
        self.station_file = os.path.join(base_dir, self.data.get("station_file", ""))

        # Output tree
        self.out_dir = os.path.join(base_dir, "work", "detection", "eqcorrscan")
        self.tmpl_dir = os.path.join(self.out_dir, "templates")   # tribe.tgz + previews
        self.log_dir = os.path.join(base_dir, "work", "logs", "eqcorrscan")
        for d in (self.out_dir, self.tmpl_dir, self.log_dir):
            os.makedirs(d, exist_ok=True)

        # ── Template creation parameters (EQcorrscan Tribe.construct) ──
        self.template_min_mag = float(self.dcfg.get("template_min_mag", 1.0))
        self.max_templates    = int(self.dcfg.get("max_templates", 20))
        self.lowcut           = float(self.dcfg.get("lowcut", 2.0))    # Hz
        self.highcut          = float(self.dcfg.get("highcut", 9.0))   # Hz
        self.samp_rate        = float(self.dcfg.get("samp_rate", 20.0))  # Hz
        self.filt_order       = int(self.dcfg.get("filt_order", 4))
        self.length           = float(self.dcfg.get("length", 3.0))    # s template window
        self.prepick          = float(self.dcfg.get("prepick", 0.15))  # s before pick
        self.swin             = str(self.dcfg.get("swin", "all"))      # P / S / all
        # Processing/segment length. The journal uses daylong (86400) but that
        # needs clean daylong data on every channel; 3600 (1 h) is a robust
        # default for gappy real SDS and is faster. Detection segments the
        # continuous scan into process_len chunks (with overlap).
        self.process_len      = int(self.dcfg.get("process_len", 3600))
        self.min_snr          = self.dcfg.get("min_snr", None)
        self.min_stations     = int(self.dcfg.get("min_stations", 3))
        # Restrict templates to the N most-used stations (cuts cost; EQcorrscan
        # tutorial does the same via filter_picks top_n_picks). 0 = keep all.
        self.top_n_stations   = int(self.dcfg.get("top_n_stations", 0))

        # ── Detection parameters (Tribe.detect) ──
        self.threshold        = float(self.dcfg.get("threshold", 8.0))
        self.threshold_type   = str(self.dcfg.get("threshold_type", "MAD"))
        self.trig_int         = float(self.dcfg.get("trig_int", 6.0))  # s min inter-detection
        self.xcorr_func       = str(self.dcfg.get("xcorr_func", "fftw"))
        self.concurrency      = str(self.dcfg.get("concurrency", "concurrent"))
        self.cores            = self.dcfg.get("cores", None)

        # ── lag-calc (optional repick) ──
        self.do_lag_calc      = bool(self.dcfg.get("lag_calc", False))
        self.lag_shift_len    = float(self.dcfg.get("lag_shift_len", 0.2))
        self.lag_min_cc       = float(self.dcfg.get("lag_min_cc", 0.4))

        # ── Scan range (empty = span the template days) ──
        self.scan_start = self.dcfg.get("scan_start") or None
        self.scan_end   = self.dcfg.get("scan_end") or None

        self._stadf = None

    # ── Stations ─────────────────────────────────────────────────────────────
    def _stations(self):
        if self._stadf is None:
            from seiswork.utils.converter import _load_station_df
            self._stadf = _load_station_df(self.station_file)
        return self._stadf

    def _station_coords(self):
        """{STA: (lat, lon, elev_m)} for the catalog-location fallback."""
        stadf = self._stations()
        out = {}
        if stadf is None or len(stadf) == 0:
            return out
        for _, r in stadf.iterrows():
            try:
                out[str(r["station"])] = (float(r["lat"]), float(r["lon"]),
                                          float(r.get("elev", 0.0) or 0.0))
            except Exception:
                continue
        return out

    # ── Catalog + picks → ObsPy Catalog with picks ───────────────────────────
    def _find_picks_file(self, catalog_file: str):
        """The picks CSV that sits next to the catalog (REAL: picks_real.csv /
        GaMMA: picks_associated.csv). Returns (path, layout) or (None, None)."""
        d = os.path.dirname(catalog_file)
        for nm, layout in (("picks_real.csv", "real"),
                           ("picks_associated.csv", "gamma"),
                           ("picks_gamma.csv", "gamma")):
            p = os.path.join(d, nm)
            if os.path.exists(p):
                return p, layout
        return None, None

    def _build_template_catalog(self, catalog_file: str):
        """Select template events + attach their picks → ObsPy Catalog.

        Returns (obspy Catalog, {event_id: {lat, lon, dep, mag}})."""
        from obspy import UTCDateTime
        from obspy.core.event import (
            Catalog, Event, Origin, Magnitude, Pick, WaveformStreamID,
            ResourceIdentifier)

        cat_df = pd.read_csv(catalog_file)
        # strongest first, cap
        cat_df = cat_df.copy()
        cat_df["_mag"] = pd.to_numeric(cat_df.get("mag"), errors="coerce")
        cat_df = cat_df[cat_df["_mag"].notna() & (cat_df["_mag"] >= self.template_min_mag)]
        cat_df = cat_df.sort_values("_mag", ascending=False).head(self.max_templates)
        if len(cat_df) == 0:
            return Catalog(), {}

        wanted_ids = set(cat_df["event_id"].astype(str))
        picks_file, layout = self._find_picks_file(catalog_file)
        picks_by_ev = {}
        top_stations = None
        if picks_file:
            pk = pd.read_csv(picks_file)
            link_col = "event_id" if layout == "real" else (
                "event_index" if "event_index" in pk.columns else "event_id")
            phase_col = "phase" if "phase" in pk.columns else "type"
            time_col = "pick_time" if "pick_time" in pk.columns else "timestamp"
            if link_col in pk.columns:
                pk = pk[pk[link_col].astype(str).isin(wanted_ids)]
                # optional: keep only the N most-used stations across templates
                if self.top_n_stations > 0 and "station" in pk.columns:
                    counts = pk["station"].astype(str).value_counts()
                    top_stations = set(counts.head(self.top_n_stations).index)
                    pk = pk[pk["station"].astype(str).isin(top_stations)]
                for eid, grp in pk.groupby(link_col):
                    rows = []
                    for _, r in grp.iterrows():
                        try:
                            rows.append({
                                "net": str(r.get("network", "") or ""),
                                "sta": str(r["station"]),
                                "loc": str(r.get("location", "") or ""),
                                "cha": str(r.get("channel", "") or ""),
                                "phase": str(r[phase_col]).upper()[:1],
                                "time": UTCDateTime(str(r[time_col])),
                            })
                        except Exception:
                            continue
                    picks_by_ev[str(eid)] = rows

        tmpl_meta = {}
        catalog = Catalog()
        for _, ev in cat_df.iterrows():
            eid = str(ev["event_id"])
            try:
                ot = UTCDateTime(str(ev["datetime"]))
                lat, lon = float(ev["lat"]), float(ev["lon"])
                dep = float(ev.get("depth_km", 10.0) or 10.0)
                mag = float(ev["_mag"])
            except Exception:
                continue
            picks_rows = picks_by_ev.get(eid, [])
            # swin filter: keep only requested phases for the template picks
            if self.swin.upper() == "P":
                picks_rows = [p for p in picks_rows if p["phase"] == "P"]
            elif self.swin.upper() == "S":
                picks_rows = [p for p in picks_rows if p["phase"] == "S"]
            if len({p["sta"] for p in picks_rows}) < self.min_stations:
                # not enough picks to build a usable template
                continue

            event = Event(resource_id=ResourceIdentifier(id=eid))
            origin = Origin(time=ot, latitude=lat, longitude=lon, depth=dep * 1000.0)
            event.origins = [origin]
            event.preferred_origin_id = origin.resource_id
            event.magnitudes = [Magnitude(mag=mag)]
            for p in picks_rows:
                # infer a channel when the picks file only stored a band (e.g. HH)
                cha = p["cha"]
                if len(cha) == 2:
                    cha = cha + ("Z" if p["phase"] == "P" else "N")
                event.picks.append(Pick(
                    time=p["time"], phase_hint=p["phase"],
                    waveform_id=WaveformStreamID(
                        network_code=p["net"] or None,
                        station_code=p["sta"],
                        location_code=p["loc"] or "",
                        channel_code=cha or None)))
            catalog.append(event)
            tmpl_meta[eid] = {"lat": lat, "lon": lon, "dep": dep, "mag": mag}
        return catalog, tmpl_meta

    # ── SDS ──────────────────────────────────────────────────────────────────
    def _sds_client(self):
        from obspy.clients.filesystem.sds import Client as SDSClient
        return SDSClient(self.sds_path)

    def _window_stream(self, t0, t1, seed_ids):
        """Load [t0, t1] of continuous data from SDS for the given seed ids."""
        from obspy import Stream
        sds = self._sds_client()
        st = Stream()
        for sid in seed_ids:
            net, sta, loc, cha = sid.split(".")
            try:
                st += sds.get_waveforms(net, sta, loc or "*", cha, t0, t1)
            except Exception:
                continue
        return st

    def _day_stream(self, day_ymd: str, seed_ids, pad_s: float = 0.0):
        """Load one UTC day (optionally padded) of continuous data from SDS."""
        from obspy import UTCDateTime
        t0 = UTCDateTime(f"{day_ymd[:4]}-{day_ymd[4:6]}-{day_ymd[6:8]}T00:00:00") - pad_s
        t1 = t0 + 86400 + 2 * pad_s
        return self._window_stream(t0, t1, seed_ids)

    @staticmethod
    def _clean_stream(st, min_coverage: float = 0.5):
        """Drop channels that are excessively gappy/zero-heavy before handing the
        stream to EQcorrscan. `template_gen`'s internal pre-processing hardcodes
        `ignore_bad_data=False`, so ONE mostly-empty channel would otherwise
        abort the whole template ('more zeros than actual data'). Merge (fill
        gaps with 0), then keep only channels with ≥`min_coverage` non-zero
        samples. Returns a cleaned Stream."""
        from obspy import Stream
        try:
            st = st.merge(method=1, fill_value=0)
        except Exception:
            st = st.merge(fill_value=0)
        good = Stream()
        for tr in st:
            data = tr.data
            if hasattr(data, "filled"):
                data = data.filled(0)
                tr.data = data
            npts = tr.stats.npts
            if npts == 0:
                continue
            if np.count_nonzero(data) / npts >= min_coverage:
                good += tr
        return good

    # ── Template creation (Tribe) ────────────────────────────────────────────
    def _build_tribe(self, catalog):
        """Construct an EQcorrscan Tribe: one Template per catalog event, cut
        from that event's day of SDS data. Per-event so one bad event (missing
        data, too-short window) doesn't sink the whole tribe."""
        from eqcorrscan.core.match_filter import Tribe, Template
        from obspy.core.event import Catalog

        tribe = Tribe()
        for event in catalog:
            eid = event.resource_id.id.split("/")[-1]
            seed_ids = {
                "{n}.{s}.{l}.{c}".format(
                    n=p.waveform_id.network_code or "*",
                    s=p.waveform_id.station_code,
                    l=p.waveform_id.location_code or "",
                    c=p.waveform_id.channel_code or "*")
                for p in event.picks}
            # Load a window of exactly process_len that comfortably contains all
            # picks (start ~60 s before the earliest pick), not the whole day —
            # this avoids day-long SDS gaps and is much faster.
            first_pick = min(p.time for p in event.picks)
            t0 = first_pick - 60.0
            t1 = t0 + self.process_len
            st = self._window_stream(t0, t1, seed_ids)
            st = self._clean_stream(st)
            if len(st) == 0:
                print(f"[EQcorrscan]   template {eid}: no usable SDS data, skipped", flush=True)
                continue
            # Bind every pick to a real channel present in the stream. Picks from
            # REAL carry no channel code (and GaMMA may store only a band), so
            # without this template_gen drops them ("Pick not associated with a
            # channel") → empty template. P → vertical (…Z), S → a horizontal.
            self._bind_pick_channels(event, st)
            try:
                t = Tribe().construct(
                    method="from_meta_file", meta_file=Catalog([event]), st=st,
                    lowcut=self.lowcut, highcut=self.highcut,
                    samp_rate=self.samp_rate, filt_order=self.filt_order,
                    length=self.length, prepick=self.prepick, swin=self.swin,
                    process_len=self.process_len,
                    min_snr=self.min_snr, parallel=False,
                    skip_short_chans=True,
                    # tolerate gappy/zero-heavy channels: drop them instead of
                    # aborting the whole template (common with real SDS data)
                    ignore_length=True, ignore_bad_data=True)
            except Exception as e:
                print(f"[EQcorrscan]   template {eid}: construct failed ({e})", flush=True)
                continue
            for tmpl in t:
                tmpl.name = eid          # keep the catalog event id as the name
                if len(tmpl.st) == 0:
                    continue
                tribe += tmpl
                print(f"[EQcorrscan]   template {eid}: {len(tmpl.st)} channels", flush=True)
        return tribe

    @staticmethod
    def _bind_pick_channels(event, st):
        """Set each pick's waveform_id.channel_code to a channel actually in the
        stream for that station (vertical …Z for P, a horizontal for S). Picks
        already matching a stream channel are left untouched."""
        # station → {"Z": chan, "H": chan} from the loaded stream
        by_sta = {}
        for tr in st:
            comp = tr.stats.channel[-1:].upper()
            slot = "Z" if comp == "Z" else ("H" if comp in ("N", "E", "1", "2") else None)
            if slot:
                by_sta.setdefault(tr.stats.station, {}).setdefault(slot, tr.stats.channel)
        for pick in event.picks:
            wid = pick.waveform_id
            sta = wid.station_code
            chans = by_sta.get(sta, {})
            if not chans:
                continue
            # already valid?
            cur = wid.channel_code or ""
            has = any(tr.stats.channel == cur and tr.stats.station == sta for tr in st)
            if cur and has:
                continue
            want = "Z" if (pick.phase_hint or "P").upper().startswith("P") else "H"
            chan = chans.get(want) or chans.get("Z") or next(iter(chans.values()))
            wid.channel_code = chan
            # match the network/location the stream actually uses
            for tr in st:
                if tr.stats.station == sta and tr.stats.channel == chan:
                    wid.network_code = tr.stats.network
                    wid.location_code = tr.stats.location
                    break

    # ── Detection scan ───────────────────────────────────────────────────────
    def _scan_days(self, catalog):
        """List of YYYYMMDD days to scan — explicit range, else the template days."""
        from obspy import UTCDateTime
        if self.scan_start and self.scan_end:
            d0 = UTCDateTime(self.scan_start)
            d1 = UTCDateTime(self.scan_end)
            days = []
            d = UTCDateTime(d0.year, d0.month, d0.day)
            while d <= d1:
                days.append(f"{d.year:04d}{d.month:02d}{d.day:02d}")
                d += 86400
            return days
        out = set()
        for event in catalog:
            ot = (event.preferred_origin() or event.origins[0]).time
            out.add(f"{ot.year:04d}{ot.month:02d}{ot.day:02d}")
        return sorted(out)

    def _scan(self, tribe, days):
        """Run the whole Tribe over each day of continuous SDS data → one merged
        Party. Data are loaded once per day and pre-processed by detect()."""
        from eqcorrscan.core.match_filter import Party
        seed_ids = sorted({tr.id for tmpl in tribe for tr in tmpl.st})
        # overlap so detections straddling midnight are not lost (max template moveout)
        max_len = max((tmpl.process_length for tmpl in tribe), default=0)
        party = Party()
        for day in days:
            print(f"[EQcorrscan] scanning {day} ({len(seed_ids)} channels)…", flush=True)
            st = self._day_stream(day, seed_ids, pad_s=60.0)
            st = self._clean_stream(st)
            if len(st) == 0:
                print(f"[EQcorrscan]   {day}: no usable data", flush=True)
                continue
            try:
                day_party = tribe.detect(
                    stream=st, threshold=self.threshold,
                    threshold_type=self.threshold_type, trig_int=self.trig_int,
                    xcorr_func=self.xcorr_func, concurrency=self.concurrency,
                    cores=self.cores, ignore_length=True, ignore_bad_data=True,
                    overlap="calculate", parallel_process=False)
            except Exception as e:
                print(f"[EQcorrscan]   {day}: detect failed ({e})", flush=True)
                continue
            n = sum(len(f) for f in day_party)
            print(f"[EQcorrscan]   {day}: {n} raw detection(s)", flush=True)
            for fam in day_party:
                party += fam
            # optional lag-calc repick on this day's stream (needs the raw data)
            if self.do_lag_calc and n:
                try:
                    day_party.lag_calc(
                        stream=st, pre_processed=False,
                        shift_len=self.lag_shift_len, min_cc=self.lag_min_cc,
                        ignore_length=True)
                except Exception as e:
                    print(f"[EQcorrscan]   {day}: lag-calc failed ({e})", flush=True)
        return party

    # ── Party → SeisWork catalog CSV ─────────────────────────────────────────
    def _party_to_catalog(self, party, tmpl_meta):
        """One row per declustered detection; hypocentre inherited from the
        template (matched-filter detections are near-repeating to their
        template), magnitude = template mag (first estimate)."""
        rows = []
        n = 0
        for family in party:
            tname = family.template.name
            meta = tmpl_meta.get(tname, {})
            lat = meta.get("lat", np.nan)
            lon = meta.get("lon", np.nan)
            dep = meta.get("dep", np.nan)
            mag = meta.get("mag", np.nan)
            for det in family:
                dt = det.detect_time
                rows.append({
                    "event_id": f"eqc_{n:06d}",
                    "datetime": dt.datetime.isoformat(timespec="milliseconds"),
                    "lat": lat, "lon": lon, "depth_km": dep,
                    "mag": mag, "rms": np.nan,
                    "nsta": int(det.no_chans),
                    "gap": np.nan, "method": "eqcorrscan",
                    # extra QC columns (kept; downstream tolerates extras)
                    "template": tname,
                    "detect_val": float(det.detect_val),
                    "threshold": float(det.threshold),
                    "channels": det.no_chans,
                    "mag_note": "template-inherited",
                })
                n += 1
        cols = CATALOG_COLS + ["template", "detect_val", "threshold",
                               "channels", "mag_note"]
        df = pd.DataFrame(rows, columns=cols)
        # chronological
        if len(df):
            df = df.sort_values("datetime").reset_index(drop=True)
            df["event_id"] = [f"eqc_{i:06d}" for i in range(len(df))]
        return df

    # ── Orchestration ────────────────────────────────────────────────────────
    def run(self, catalog_file: str):
        t0 = time.time()
        if not os.path.exists(catalog_file):
            print(f"[EQcorrscan] ERROR: template catalog not found: {catalog_file}")
            return None
        if not (self.sds_path and os.path.isdir(self.sds_path)):
            print(f"[EQcorrscan] ERROR: SDS archive not found: {self.sds_path}")
            return None

        print(f"[EQcorrscan] Matched-filter detection (Chamberlain et al. 2018)")
        print(f"[EQcorrscan] SDS: {self.sds_path}")
        print(f"[EQcorrscan] template catalog: {catalog_file}")

        # 1) template creation
        print(f"[EQcorrscan] Stage 1 — building templates "
              f"(mag≥{self.template_min_mag}, ≤{self.max_templates}, "
              f"{self.lowcut}-{self.highcut} Hz, {self.length}s, swin={self.swin})…", flush=True)
        catalog, tmpl_meta = self._build_template_catalog(catalog_file)
        if len(catalog) == 0:
            print(f"[EQcorrscan] No catalog events with mag ≥ {self.template_min_mag} "
                  f"and ≥{self.min_stations} picks. Nothing to do.")
            return None
        tribe = self._build_tribe(catalog)
        if len(tribe) == 0:
            print("[EQcorrscan] No usable templates could be built (check SDS coverage "
                  "for the template days & picks). Nothing to do.")
            return None
        print(f"[EQcorrscan] Tribe: {len(tribe)} template(s).", flush=True)
        try:
            tribe.write(os.path.join(self.tmpl_dir, "tribe"))
        except Exception as e:
            print(f"[EQcorrscan]   (could not save tribe: {e})", flush=True)

        # 2) detection scan
        days = self._scan_days(catalog)
        print(f"[EQcorrscan] Stage 2 — scanning {len(days)} day(s): "
              f"{days[0]}…{days[-1]} (threshold {self.threshold} {self.threshold_type}, "
              f"trig_int {self.trig_int}s)…", flush=True)
        party = self._scan(tribe, days)

        # 3) decluster
        n_raw = sum(len(f) for f in party)
        if n_raw and len(party) > 0:
            try:
                party.decluster(trig_int=self.trig_int, timing="detect",
                                metric="cor_sum")
            except Exception as e:
                print(f"[EQcorrscan]   decluster failed ({e})", flush=True)
        n_dec = sum(len(f) for f in party)
        print(f"[EQcorrscan] Stage 3 — declustered {n_raw} → {n_dec} detection(s).", flush=True)

        # 4) → catalog
        cat_out = self._party_to_catalog(party, tmpl_meta)
        self._write_outputs(cat_out, party, t0)
        return cat_out

    def _write_outputs(self, catalog: pd.DataFrame, party, t0: float):
        out_csv = os.path.join(self.out_dir, "catalog_eqcorrscan.csv")
        catalog.to_csv(out_csv, index=False)
        cdir = os.path.join(self.base_dir, "work", "catalog")
        os.makedirs(cdir, exist_ok=True)
        catalog.to_csv(os.path.join(cdir, "catalog_eqcorrscan.csv"), index=False)
        # the full Party (with per-detection events/picks) for later lag-calc/relocation
        try:
            if sum(len(f) for f in party) > 0:
                party.write(os.path.join(self.out_dir, "party"), overwrite=True)
        except Exception as e:
            print(f"[EQcorrscan]   (could not save party: {e})", flush=True)
        print(f"[EQcorrscan] Done. {len(catalog)} detection(s) in "
              f"{time.time() - t0:.1f}s → {out_csv}", flush=True)

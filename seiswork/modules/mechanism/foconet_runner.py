"""
SeisWork - Focal mechanism via FocoNet (transformer deep-learning) - by HakimBMKG

FocoNet (Song et al., 2026, JGR-ML) predicts focal mechanisms (PTB axes ->
strike/dip/rake) from per-station seismic features.

Two variants are supported, selected automatically:

  FocoNet_Full  - 11 features/station: polarity + S/P amplitude ratios + SNR
                  (requires 3-component HHZ/HHN/HHE in SDS + S picks)
  FocoNet_O     - 1 feature/station: polarity only (fallback)

FocoNet_Full is always preferred when S picks and 3-component data are available.
"""

import os
import sys

import numpy as np
import pandas as pd
import torch

_HERE = os.path.dirname(os.path.abspath(__file__))

# ── FocoNet_Full ──────────────────────────────────────────────────────────────
FOCONET_FULL_DIR     = os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "core", "src", "FocoNet", "FocoNet_Full"))
FOCONET_FULL_WEIGHTS = os.path.join(FOCONET_FULL_DIR, "model", "FocoNet_Full.pth")

# ── FocoNet_O (polarity-only fallback) ───────────────────────────────────────
FOCONET_O_DIR     = os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "core", "src", "FocoNet", "FocoNet_O"))
FOCONET_O_WEIGHTS = os.path.join(FOCONET_O_DIR, "model", "FocoNet_O.pth")

MAX_STA      = 32
KM_PER_DEG   = 111.195
_FULL_NCOLS  = 11   # FocoNet_Full polarity tensor width
_O_NCOLS     = 1    # FocoNet_O polarity tensor width


def _haversine_km(lat0, lon0, lat1, lon1):
    dlat = lat1 - lat0
    dlon = lon1 - lon0
    lat_mid = np.radians((lat0 + lat1) / 2.0)
    dy = dlat * KM_PER_DEG
    dx = dlon * KM_PER_DEG * np.cos(lat_mid)
    return float(dx), float(dy)


# ── Model loaders (cached per process) ───────────────────────────────────────

def _load_foconet_full(device):
    if not hasattr(_load_foconet_full, "_cache"):
        if FOCONET_FULL_DIR not in sys.path:
            sys.path.insert(0, FOCONET_FULL_DIR)
        import importlib, model as _FM
        importlib.reload(_FM)
        net = _FM.FocoNet()
        state = torch.load(FOCONET_FULL_WEIGHTS, map_location=device, weights_only=True)
        net.load_state_dict(state)
        net.eval().to(device)
        _load_foconet_full._cache = net
    return _load_foconet_full._cache


def _load_foconet_o(device):
    if not hasattr(_load_foconet_o, "_cache"):
        # FocoNet_O uses the same module names, so it must load from its own
        # dir. Prepend FocoNet_O dir so its model.py is found first.
        if FOCONET_O_DIR not in sys.path:
            sys.path.insert(0, FOCONET_O_DIR)
        # If Full was already imported, reload from O dir
        import importlib
        if "model" in sys.modules:
            del sys.modules["model"]
        import model as _FM_O
        net = _FM_O.FocoNet()
        state = torch.load(FOCONET_O_WEIGHTS, map_location=device, weights_only=True)
        net.load_state_dict(state)
        net.eval().to(device)
        _load_foconet_o._cache = net
    return _load_foconet_o._cache


def _ptb_to_sdr(ptb_np: np.ndarray):
    try:
        from pyrocko import moment_tensor as pmt
        P = ptb_np[0] / (np.linalg.norm(ptb_np[0]) + 1e-9)
        T = ptb_np[1] / (np.linalg.norm(ptb_np[1]) + 1e-9)
        mt = pmt.MomentTensor(p_axis=P, t_axis=T)
        s, d, r = mt.both_strike_dip_rake()[0]
        return float(s), float(d), float(r)
    except Exception as e:
        print(f"[FOCONET] PTB→SDR error: {e}", flush=True)
        return None, None, None


# ── Tensor builders ───────────────────────────────────────────────────────────

def _build_tensors_o(cat_df, pol_df, station_locs):
    """FocoNet_O: polarity tensor shape (N, 32, 1)."""
    N = len(cat_df)
    pol_all   = np.zeros((N, MAX_STA, _O_NCOLS), dtype=np.float32)
    spa_all   = np.zeros((N, MAX_STA, 3),         dtype=np.float32)
    snum_all  = np.zeros((N, 1),                  dtype=np.float32)
    smask_all = np.zeros((N, MAX_STA, 1),         dtype=np.float32)
    spa_all[:, :, 2] = -100.0   # padding sentinel

    event_ids    = []
    station_rows = []

    for i, (_, ev) in enumerate(cat_df.iterrows()):
        eid    = str(ev["event_id"])
        ev_lat = float(ev["lat"])
        ev_lon = float(ev["lon"])
        ev_dep = float(ev.get("depth_km", 10.0))

        evpol = pol_df[pol_df["event_id"].astype(str) == eid]
        evpol = evpol[evpol["p_polarity"].notna()]

        ev_srows = []
        j = 0
        for _, pr in evpol.iterrows():
            if j >= MAX_STA:
                break
            sta = str(pr["station"])
            if sta not in station_locs:
                continue
            s_lat, s_lon = station_locs[sta]
            dx, dy = _haversine_km(ev_lat, ev_lon, s_lat, s_lon)
            pol_val = float(pr["p_polarity"])
            pol_all[i, j, 0]   = pol_val
            spa_all[i, j, 0]   = dx / 50.0
            spa_all[i, j, 1]   = dy / 50.0
            spa_all[i, j, 2]   = ev_dep / 20.0
            smask_all[i, j, 0] = 1.0
            ev_srows.append({
                "station": sta, "lat": s_lat, "lon": s_lon,
                "polarity": pol_val, "snr": float(pr.get("snr") or 0),
                "dx_km": dx, "dy_km": dy,
            })
            j += 1
        snum_all[i, 0] = max(j, 1)
        event_ids.append(eid)
        station_rows.append(ev_srows)

    return (torch.from_numpy(pol_all), torch.from_numpy(spa_all),
            torch.from_numpy(snum_all), torch.from_numpy(smask_all),
            event_ids, station_rows)


def _build_tensors_full(cat_df, pol_df, sp_feat_df, station_locs):
    """FocoNet_Full: polarity tensor shape (N, 32, 11)."""
    from seiswork.modules.mechanism.sp_features import SP_FEAT_COLS

    N = len(cat_df)
    pol_all   = np.zeros((N, MAX_STA, _FULL_NCOLS), dtype=np.float32)
    spa_all   = np.zeros((N, MAX_STA, 3),            dtype=np.float32)
    snum_all  = np.zeros((N, 1),                     dtype=np.float32)
    smask_all = np.zeros((N, MAX_STA, 1),            dtype=np.float32)
    spa_all[:, :, 2] = -100.0

    # Lookup: (event_id, station) -> sp features row
    sp_lut = {}
    if sp_feat_df is not None:
        for _, r in sp_feat_df.iterrows():
            if r.get("ok", False):
                sp_lut[(str(r["event_id"]), str(r["station"]))] = r

    event_ids    = []
    station_rows = []

    for i, (_, ev) in enumerate(cat_df.iterrows()):
        eid    = str(ev["event_id"])
        ev_lat = float(ev["lat"])
        ev_lon = float(ev["lon"])
        ev_dep = float(ev.get("depth_km", 10.0))

        evpol = pol_df[pol_df["event_id"].astype(str) == eid]
        evpol = evpol[evpol["p_polarity"].notna()]

        ev_srows = []
        j = 0
        for _, pr in evpol.iterrows():
            if j >= MAX_STA:
                break
            sta = str(pr["station"])
            if sta not in station_locs:
                continue
            s_lat, s_lon = station_locs[sta]
            dx, dy = _haversine_km(ev_lat, ev_lon, s_lat, s_lon)
            pol_val = float(pr["p_polarity"])

            # Feature 0: polarity
            pol_all[i, j, 0] = pol_val
            # Features 1-10: S/P ratios + SNR
            sp_row = sp_lut.get((eid, sta))
            if sp_row is not None:
                for k, col in enumerate(SP_FEAT_COLS):
                    pol_all[i, j, k + 1] = float(sp_row.get(col, 0.0))
            # Missing sp features: leave as 0.0 (neutral log10 ratio)

            spa_all[i, j, 0]   = dx / 50.0
            spa_all[i, j, 1]   = dy / 50.0
            spa_all[i, j, 2]   = ev_dep / 20.0
            smask_all[i, j, 0] = 1.0
            ev_srows.append({
                "station": sta, "lat": s_lat, "lon": s_lon,
                "polarity": pol_val, "snr": float(pr.get("snr") or 0),
                "dx_km": dx, "dy_km": dy,
            })
            j += 1
        snum_all[i, 0] = max(j, 1)
        event_ids.append(eid)
        station_rows.append(ev_srows)

    return (torch.from_numpy(pol_all), torch.from_numpy(spa_all),
            torch.from_numpy(snum_all), torch.from_numpy(smask_all),
            event_ids, station_rows)


# ── Main runner class ─────────────────────────────────────────────────────────

class FocoNetRunner:
    """Run FocoNet focal mechanism inference for all events in a SeisWork catalog.

    Automatically selects FocoNet_Full (11 features) when S picks and
    3-component SDS waveforms are available; otherwise falls back to
    FocoNet_O (polarity-only).
    """

    def __init__(self, cfg: dict, base_dir):
        self.cfg      = cfg
        self.base_dir = str(base_dir)
        self.fcfg     = cfg.get("mechanism", {}).get("foconet", {})

        self.mech_dir = os.path.join(self.base_dir, "work", "mechanism")
        os.makedirs(self.mech_dir, exist_ok=True)
        self.out_dir = os.path.join(self.mech_dir, "foconetout")
        os.makedirs(self.out_dir, exist_ok=True)

        self.device = torch.device(
            self.fcfg.get("device",
                          "cuda" if torch.cuda.is_available() else "cpu"))

    @staticmethod
    def _load_station_locs(inventory_path: str, network: str) -> dict:
        from obspy import read_inventory
        inv = read_inventory(inventory_path)
        net = next((n for n in inv if n.code == network), None)
        if net is None:
            return {}
        return {sta.code: (sta.latitude, sta.longitude) for sta in net.stations}

    def run(self, catalog_csv: str, picks_csv: str, sds_path: str,
            inventory_path: str, network: str = "7G", channel=None,
            pol_df: pd.DataFrame = None,
            picks_p_df: pd.DataFrame = None,
            picks_s_df: pd.DataFrame = None) -> pd.DataFrame:
        """Run FocoNet on every event in catalog_csv.

        channel     : explicit SEED channel code to force, or None (default)
                      to auto-try the standard HH > BH > EH > SH priority
                      per station
        pol_df      : pre-computed polarity DataFrame (pass from SKHASH pipeline
                      step to avoid re-reading waveforms)
        picks_p_df  : P-pick DataFrame - needed for FocoNet_Full S/P features
                      when pol_df is provided without picks_csv
        picks_s_df  : S-pick DataFrame - triggers FocoNet_Full when available
        """
        from seiswork.modules.mechanism.polarity import compute_polarities

        cat_df = pd.read_csv(catalog_csv)

        # ── polarities ───────────────────────────────────────────────────────
        if pol_df is None:
            picks_df  = pd.read_csv(picks_csv)
            picks_p   = picks_df[picks_df.phase == "P"].copy()
            picks_s_c = picks_df[picks_df.phase == "S"].copy()
            if "network" not in picks_p.columns:
                picks_p["network"] = network
            if "network" not in picks_s_c.columns:
                picks_s_c["network"] = network
            print(f"[FOCONET] computing polarities for {len(picks_p)} P picks...",
                  flush=True)
            pol_df = compute_polarities(picks_p, sds_path, channel=channel)
            if picks_s_df is None:
                picks_s_df = picks_s_c
            if picks_p_df is None:
                picks_p_df = picks_p
        else:
            # pol_df was provided externally; ensure network column exists
            if picks_p_df is not None and "network" not in picks_p_df.columns:
                picks_p_df = picks_p_df.copy()
                picks_p_df["network"] = network

        n_ok = int(pol_df["p_polarity"].notna().sum())
        print(f"[FOCONET] {n_ok} usable polarities over {len(cat_df)} events",
              flush=True)

        station_locs = self._load_station_locs(inventory_path, network)
        if not station_locs:
            raise RuntimeError(
                f"No stations for network {network} in {inventory_path}")

        # ── decide variant ────────────────────────────────────────────────────
        use_full = (
            os.path.exists(FOCONET_FULL_WEIGHTS) and
            picks_s_df is not None and len(picks_s_df) > 0 and
            picks_p_df is not None and len(picks_p_df) > 0 and
            bool(sds_path)
        )

        if use_full:
            print("[FOCONET] variant: FocoNet_Full "
                  "(polarity + S/P ratios + SNR, 11 features/station)", flush=True)
            try:
                from seiswork.modules.mechanism.sp_features import compute_sp_features
                sp_feat_df = compute_sp_features(
                    picks_p_df, picks_s_df, sds_path, station_locs, cat_df,
                    network=network)
                n_sp_ok = int(sp_feat_df["ok"].sum()) if "ok" in sp_feat_df.columns else 0
                print(f"[FOCONET] S/P features: {n_sp_ok}/{len(sp_feat_df)} stations OK",
                      flush=True)
                pol_t, spa_t, snum_t, smask_t, event_ids, station_rows = \
                    _build_tensors_full(cat_df, pol_df, sp_feat_df, station_locs)
                print("[FOCONET] loading FocoNet_Full model...", flush=True)
                net    = _load_foconet_full(self.device)
                method = "FocoNet_Full"
            except Exception as _e_full:
                import traceback as _tb
                print(f"[FOCONET] FocoNet_Full failed ({_e_full}) — falling back to FocoNet_O",
                      flush=True)
                _tb.print_exc()
                use_full = False  # redirect to FocoNet_O block below
        if not use_full:
            if not use_full and os.path.exists(FOCONET_FULL_WEIGHTS):
                print("[FOCONET] FocoNet_Full weights found but S picks or SDS path "
                      "unavailable — falling back to FocoNet_O", flush=True)
            print("[FOCONET] variant: FocoNet_O (polarity-only, 1 feature/station)",
                  flush=True)
            pol_t, spa_t, snum_t, smask_t, event_ids, station_rows = \
                _build_tensors_o(cat_df, pol_df, station_locs)
            print("[FOCONET] loading FocoNet_O model...", flush=True)
            net    = _load_foconet_o(self.device)
            method = "FocoNet_O"

        # ── inference ─────────────────────────────────────────────────────────
        pol_t   = pol_t.to(self.device)
        spa_t   = spa_t.to(self.device)
        snum_t  = snum_t.to(self.device)
        smask_t = smask_t.to(self.device)

        print(f"[FOCONET] running inference on {len(event_ids)} events "
              f"({method})...", flush=True)
        with torch.no_grad():
            ptb_out = net(pol_t, spa_t, snum_t, smask_t, mode="test")

        ptb_np = ptb_out.reshape(-1, 3, 3).cpu().numpy()

        # ── build results ─────────────────────────────────────────────────────
        ev_index = {str(r["event_id"]): r for _, r in cat_df.iterrows()}
        rows = []
        for i, eid in enumerate(event_ids):
            n_pol = int(snum_t[i, 0].item())
            if n_pol < 1:
                continue
            strike, dip, rake = _ptb_to_sdr(ptb_np[i])
            if strike is None:
                continue
            ev = ev_index.get(eid, {})
            rows.append({
                "event_id":        eid,
                "strike":          round(strike, 1),
                "dip":             round(dip,    1),
                "rake":            round(rake,   1),
                "n_pol":           n_pol,
                "origin_lat":      float(ev.get("lat",      0)),
                "origin_lon":      float(ev.get("lon",      0)),
                "origin_depth_km": float(ev.get("depth_km", 0)),
                "magnitude":       float(ev.get("mag",      0) or 0),
                "time":            str(ev.get("datetime", "")),
                "method":          method,
            })

        result_df = pd.DataFrame(rows)
        out_csv   = os.path.join(self.out_dir, "foconet_out.csv")
        result_df.to_csv(out_csv, index=False)
        print(f"[FOCONET] {len(result_df)} solutions → {out_csv}", flush=True)

        # ── station_rows.json (before beachball plots) ─────────────────────
        import json
        sta_map = {eid: station_rows[i] for i, eid in enumerate(event_ids)}
        with open(os.path.join(self.out_dir, "station_rows.json"), "w") as f:
            json.dump(sta_map, f)

        # ── beachball plots ────────────────────────────────────────────────
        from seiswork.modules.mechanism.beachball_plot import plot_beachball_foconet
        eid_to_srows = {eid: station_rows[i] for i, eid in enumerate(event_ids)}
        n_plots = 0
        for _, row in result_df.iterrows():
            eid   = str(row["event_id"])
            srows = eid_to_srows.get(eid, [])
            pol_info = pd.DataFrame(srows) if srows else None
            plot_beachball_foconet(
                row["strike"], row["dip"], row["rake"],
                os.path.join(self.out_dir, f"{eid}.png"),
                pol_df=pol_info)
            n_plots += 1
        print(f"[FOCONET] {n_plots} beachball plots rendered ({method}).",
              flush=True)

        return result_df

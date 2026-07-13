#!/usr/bin/env python3
"""
SeisWork - Visualization utilities
Author : HakimBMKG

Produces standardized maps and figures for each pipeline step.
"""

import os
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")


class SeisPlotter:
    """Generate maps and figures for SeisWork pipeline results."""

    def __init__(self, cfg: dict, base_dir: str):
        self.cfg      = cfg
        self.base_dir = base_dir
        self.pcfg     = cfg.get("plot", {})
        self.reg      = cfg["region"]
        self.plot_dir = os.path.join(base_dir, "work", "plots")
        os.makedirs(self.plot_dir, exist_ok=True)

        self.dpi     = int(self.pcfg.get("dpi", 150))
        self.figsize = tuple(self.pcfg.get("figsize", [12, 10]))
        self.cmap    = self.pcfg.get("depth_colormap", "viridis_r")
        self.mag_sc  = float(self.pcfg.get("mag_scale", 3.0))

    def _get_extent(self):
        r = self.reg
        ext = self.pcfg.get("map_extent", None)
        if ext:
            return ext
        pad = 0.1
        return [r["lon_min"]-pad, r["lon_max"]+pad,
                r["lat_min"]-pad, r["lat_max"]+pad]

    def _base_map(self, ax, title: str = ""):
        """Add coastlines, grid, and title to cartopy axes."""
        try:
            import cartopy.crs as ccrs
            import cartopy.feature as cfeature
            ax.add_feature(cfeature.COASTLINE.with_scale("10m"), linewidth=0.8)
            ax.add_feature(cfeature.BORDERS, linewidth=0.5, linestyle=":")
            ax.add_feature(cfeature.LAND, color="#f5f0e8")
            ax.add_feature(cfeature.OCEAN, color="#cce5ff")
            ax.gridlines(draw_labels=True, dms=False, x_inline=False, y_inline=False,
                         linewidth=0.4, color="gray", alpha=0.5)
        except ImportError:
            ax.set_xlabel("Longitude (°E)")
            ax.set_ylabel("Latitude (°N)")
        if title:
            ax.set_title(title, fontsize=11, fontweight="bold")
        ext = self._get_extent()
        ax.set_extent(ext) if hasattr(ax, "set_extent") else ax.set_xlim(ext[:2])
        if hasattr(ax, "set_ylim"):
            ax.set_ylim(ext[2:])

    def _load_stations(self) -> pd.DataFrame:
        sta_file = os.path.join(self.base_dir, self.cfg["data"]["station_file"])
        if not os.path.exists(sta_file):
            return pd.DataFrame()
        try:
            df = pd.read_csv(sta_file, sep="|", header=None,
                             names=["network","station","lat","lon","elev"],
                             usecols=[0,1,2,3,4])
        except Exception:
            df = pd.read_csv(sta_file, sep=r"\s+", header=None,
                             names=["station","lat","lon","elev"])
        return df

    # ── Plot picks distribution ───────────────────────────────────────────────
    def plot_picks(self):
        import matplotlib.pyplot as plt
        picks_file = os.path.join(self.base_dir, "work", "picks", "picks.csv")
        if not os.path.exists(picks_file):
            print("[Plot] picks.csv not found — skipping.")
            return

        df = pd.read_csv(picks_file)
        df.columns = [c.lower() for c in df.columns]
        time_col = next((c for c in df.columns if "time" in c), None)
        if time_col:
            df[time_col] = pd.to_datetime(df[time_col], errors="coerce", utc=True)

        fig, axes = plt.subplots(1, 2, figsize=self.figsize)

        # Phase type distribution
        ph_col = next((c for c in ["phase_hint","type","phase"] if c in df.columns), None)
        if ph_col:
            df[ph_col].str.upper().str[0].value_counts().plot.bar(ax=axes[0], color=["#2196F3","#FF5722","gray"])
            axes[0].set_title("Phase picks per type")
            axes[0].set_xlabel("Phase")
            axes[0].set_ylabel("Count")

        # Picks per station
        sta_col = next((c for c in ["station","sta"] if c in df.columns), None)
        if sta_col:
            df[sta_col].value_counts().head(30).plot.barh(ax=axes[1])
            axes[1].set_title("Picks per station (top 30)")
            axes[1].invert_yaxis()

        plt.tight_layout()
        out = os.path.join(self.plot_dir, "picks_summary.png")
        fig.savefig(out, dpi=self.dpi, bbox_inches="tight")
        plt.close(fig)
        print(f"[Plot] → {out}")

    # ── Plot earthquake catalog ───────────────────────────────────────────────
    def plot_catalog(self, cat_file: str, tag: str = "catalog"):
        import matplotlib.pyplot as plt
        if not os.path.exists(cat_file):
            print(f"[Plot] {cat_file} not found — skipping.")
            return

        cat      = pd.read_csv(cat_file)
        stations = self._load_stations()
        cat["depth_km"] = pd.to_numeric(cat["depth_km"], errors="coerce")
        cat["mag"]      = pd.to_numeric(cat.get("mag", 0), errors="coerce").fillna(0)

        try:
            import cartopy.crs as ccrs
            proj = ccrs.PlateCarree()
            fig, ax = plt.subplots(figsize=self.figsize, subplot_kw={"projection": proj})
        except ImportError:
            fig, ax = plt.subplots(figsize=self.figsize)
            proj = None

        self._base_map(ax, title=f"Seismicity Map ({tag})\n"
                       f"N={len(cat)}  Region: {self.reg['name']}")

        # Plot events
        kw = {"transform": proj} if proj else {}
        sc = ax.scatter(
            cat["lon"], cat["lat"],
            c=cat["depth_km"],
            s=np.clip(cat["mag"] * self.mag_sc, 2, 100) ** 2 / 10,
            cmap=self.cmap, vmin=0, vmax=self.reg.get("depth_max", 60),
            alpha=0.7, linewidths=0.3, edgecolors="k",
            label="Events", **kw
        )
        plt.colorbar(sc, ax=ax, label="Depth (km)", shrink=0.6, pad=0.02)

        # Plot stations
        if not stations.empty:
            ax.scatter(stations["lon"], stations["lat"],
                       marker="^", s=60, c="red", zorder=5,
                       label="Stations", **kw)
            for _, r in stations.iterrows():
                ax.text(r["lon"], r["lat"]+0.015, r["station"],
                        fontsize=5, ha="center", **kw)

        ax.legend(fontsize=8, loc="lower right")
        plt.tight_layout()
        out = os.path.join(self.plot_dir, f"map_{tag}.png")
        fig.savefig(out, dpi=self.dpi, bbox_inches="tight")
        plt.close(fig)
        print(f"[Plot] → {out}")

        # Depth histogram
        fig2, ax2 = plt.subplots(figsize=(8, 4))
        cat["depth_km"].dropna().hist(bins=40, ax=ax2, color="#2196F3", edgecolor="k", alpha=0.8)
        ax2.set_xlabel("Depth (km)")
        ax2.set_ylabel("Count")
        ax2.set_title(f"Depth distribution — {tag}")
        plt.tight_layout()
        out2 = os.path.join(self.plot_dir, f"depth_hist_{tag}.png")
        fig2.savefig(out2, dpi=self.dpi, bbox_inches="tight")
        plt.close(fig2)
        print(f"[Plot] → {out2}")

    # ── Before/after relocation comparison ───────────────────────────────────
    def plot_relocation(self):
        import matplotlib.pyplot as plt
        located  = os.path.join(self.base_dir, "work", "catalog", "catalog_located.csv")
        relocated = os.path.join(self.base_dir, "work", "catalog", "catalog_relocated.csv")

        dfs = {}
        for tag, fp in [("Initial", located), ("Relocated", relocated)]:
            if os.path.exists(fp):
                dfs[tag] = pd.read_csv(fp)

        if not dfs:
            print("[Plot] No relocation catalogs found.")
            return

        try:
            import cartopy.crs as ccrs
            proj = ccrs.PlateCarree()
            fig, axes = plt.subplots(1, len(dfs), figsize=(self.figsize[0]*len(dfs)//2+4, self.figsize[1]),
                                      subplot_kw={"projection": proj})
        except ImportError:
            fig, axes = plt.subplots(1, len(dfs), figsize=(self.figsize[0], self.figsize[1]))
            proj = None

        if len(dfs) == 1:
            axes = [axes]

        for ax, (tag, cat) in zip(axes, dfs.items()):
            cat["depth_km"] = pd.to_numeric(cat["depth_km"], errors="coerce")
            cat["mag"]      = pd.to_numeric(cat.get("mag", 0), errors="coerce").fillna(0)
            kw = {"transform": proj} if proj else {}
            self._base_map(ax, title=f"{tag}  (N={len(cat)})")
            sc = ax.scatter(cat["lon"], cat["lat"],
                            c=cat["depth_km"],
                            s=np.clip(cat["mag"] * self.mag_sc, 2, 100) ** 2 / 10,
                            cmap=self.cmap, vmin=0,
                            vmax=self.reg.get("depth_max", 60),
                            alpha=0.7, linewidths=0.2, edgecolors="k", **kw)
            plt.colorbar(sc, ax=ax, label="Depth (km)", shrink=0.6, pad=0.02)

        plt.tight_layout()
        out = os.path.join(self.plot_dir, "map_relocation_comparison.png")
        fig.savefig(out, dpi=self.dpi, bbox_inches="tight")
        plt.close(fig)
        print(f"[Plot] → {out}")

    # ── Summary comparison of all methods ────────────────────────────────────
    def plot_comparison(self):
        import matplotlib.pyplot as plt
        import glob

        cat_dir = os.path.join(self.base_dir, "work", "catalog")
        csv_files = sorted(glob.glob(os.path.join(cat_dir, "catalog_*.csv")))
        if not csv_files:
            print("[Plot] No catalog files found.")
            return

        fig, axes = plt.subplots(1, 2, figsize=(14, 5))
        for fp in csv_files:
            tag = os.path.basename(fp).replace("catalog_","").replace(".csv","")
            try:
                df = pd.read_csv(fp)
                depth = pd.to_numeric(df["depth_km"], errors="coerce").dropna()
                mag   = pd.to_numeric(df.get("mag",pd.Series()), errors="coerce").dropna()
                if len(depth) > 0:
                    depth.hist(bins=40, ax=axes[0], alpha=0.6, label=tag, density=True)
                if len(mag) > 0:
                    mag.hist(bins=30, ax=axes[1], alpha=0.6, label=tag, density=True)
            except Exception:
                pass

        axes[0].set_xlabel("Depth (km)"); axes[0].set_title("Depth distribution")
        axes[1].set_xlabel("Magnitude (ML)"); axes[1].set_title("Magnitude distribution")
        for ax in axes:
            ax.legend(fontsize=7)
        plt.tight_layout()
        out = os.path.join(self.plot_dir, "catalog_comparison.png")
        fig.savefig(out, dpi=self.dpi, bbox_inches="tight")
        plt.close(fig)
        print(f"[Plot] → {out}")

"""
SeisWork - Focal mechanism beachball plot (red/white, pseudo-3D shaded
sphere, transparent background) - by HakimBMKG

Replaces SKHASH's own plot_mech.py output (flat gray/black-white quadrants on
a plain white matplotlib canvas) with the same underlying geometry (reuses
SKHASH's plot_dc()/aux_plane() projection math, not reimplemented) rendered
as a shaded sphere: since the focal sphere really is a sphere, a
diffuse-lighting shade computed from each pixel's position on the unit
sphere (normal == position, centered at origin) gives a genuine 3D look from
a single 2D image, with background pixels fully transparent (alpha=0)
instead of plain white.
"""

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from SKHASH.functions.plot_mech import plot_dc, takeoff_az2xy

FACECOLOR = "#dc2626"   # red - compressional quadrants (SKHASH)
BGCOLOR   = "#ffffff"   # white - dilatational quadrants

# FocoNet colour scheme: blue compressional / white dilatational
FN_FACECOLOR = "#1e40af"   # deep-blue compressional quadrants (FocoNet)
FN_BGCOLOR   = "#ffffff"   # white dilatational quadrants


def plot_beachball_3d(strike, dip, rake, out_path, pol_df=None,
                       size_px=500, dpi=150, light_dir=(-1, -1, 2)):
    """Render one focal mechanism as a red/white shaded-sphere PNG with a
    transparent background. pol_df (optional): DataFrame with columns
    [p_polarity, takeoff, azimuth] to overlay the actual polarity picks used
    (same convention as SKHASH: + for up/compressional, o for down/dilatational).
    """
    collect_colors, patches = plot_dc(strike, dip, rake)
    path_b, path_w = (p.get_path() for p in patches)

    n = size_px
    lin = np.linspace(-1, 1, n)
    X, Y = np.meshgrid(lin, lin)
    R2 = X**2 + Y**2
    inside = R2 <= 1.0
    Z = np.zeros_like(X)
    Z[inside] = np.sqrt(1.0 - R2[inside])

    pts = np.column_stack([Y.ravel(), X.ravel()])  # plot_dc patches built from (y,x)
    in_b = path_b.contains_points(pts).reshape(X.shape)
    in_w = path_w.contains_points(pts).reshape(X.shape)

    rgb = np.ones((n, n, 3))
    fc = np.array(matplotlib.colors.to_rgb(FACECOLOR))
    bc = np.array(matplotlib.colors.to_rgb(BGCOLOR))
    rgb[in_b] = fc
    rgb[in_w & ~in_b] = bc
    rgb[~in_b & ~in_w] = bc  # anything unclassified inside the circle defaults to bg color

    light = np.array(light_dir, dtype=float)
    light = light / np.linalg.norm(light)
    normal = np.stack([X, Y, Z], axis=-1)
    diffuse = np.clip(normal @ light, 0, 1)
    shade = 0.45 + 0.55 * diffuse  # ambient + diffuse, keeps shadows from going pure black
    shade = np.where(inside, shade, 1.0)[..., None]

    rgba = np.concatenate([rgb * shade, inside.astype(float)[..., None]], axis=-1)

    fig, ax = plt.subplots(figsize=(size_px / dpi, size_px / dpi), dpi=dpi)
    fig.patch.set_alpha(0)
    ax.set_facecolor("none")
    ax.imshow(rgba, extent=[-1, 1, -1, 1], origin="lower", interpolation="bilinear", zorder=0)

    rim_shade = np.clip(np.linspace(1, 0.3, 50), 0, 1)
    theta = np.linspace(0, 2 * np.pi, 200)
    ax.plot(np.cos(theta), np.sin(theta), color="#1f1f1f", linewidth=1.1, zorder=3)

    if pol_df is not None and len(pol_df):
        xy = takeoff_az2xy(pol_df["takeoff"].values, pol_df["azimuth"].values)
        up = pol_df["p_polarity"].values > 0
        down = pol_df["p_polarity"].values < 0
        if up.any():
            ax.scatter(xy[up, 0], xy[up, 1], marker="+", s=60, linewidths=1.1, c="k", zorder=4)
        if down.any():
            ax.scatter(xy[down, 0], xy[down, 1], marker="o", s=45, linewidths=0.8,
                       edgecolor="k", facecolor="none", zorder=4)

    ax.set_xlim(-1.05, 1.05)
    ax.set_ylim(-1.05, 1.05)
    ax.set_aspect(1)
    ax.axis("off")
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    fig.savefig(out_path, dpi=dpi, transparent=True)
    plt.close(fig)


def plot_beachball_foconet(strike, dip, rake, out_path, pol_df=None,
                           size_px=500, dpi=150, light_dir=(-1, -1, 2)):
    """Blue/white shaded-sphere beachball for FocoNet results.
    pol_df (optional): DataFrame with columns [station, polarity, dx_km, dy_km]
    to overlay polarity picks (same +/o convention as SKHASH beachball).
    dx_km/dy_km are east/north offsets from event - used to compute azimuth only
    (takeoff not available from FocoNet_O directly, so omit takeoff markers).
    """
    collect_colors, patches = plot_dc(strike, dip, rake)
    path_b, path_w = (p.get_path() for p in patches)

    n = size_px
    lin = np.linspace(-1, 1, n)
    X, Y = np.meshgrid(lin, lin)
    R2 = X**2 + Y**2
    inside = R2 <= 1.0
    Z = np.zeros_like(X)
    Z[inside] = np.sqrt(1.0 - R2[inside])

    pts = np.column_stack([Y.ravel(), X.ravel()])
    in_b = path_b.contains_points(pts).reshape(X.shape)
    in_w = path_w.contains_points(pts).reshape(X.shape)

    rgb = np.ones((n, n, 3))
    fc = np.array(matplotlib.colors.to_rgb(FN_FACECOLOR))
    bc = np.array(matplotlib.colors.to_rgb(FN_BGCOLOR))
    rgb[in_b] = fc
    rgb[in_w & ~in_b] = bc
    rgb[~in_b & ~in_w] = bc

    light = np.array(light_dir, dtype=float)
    light = light / np.linalg.norm(light)
    normal = np.stack([X, Y, Z], axis=-1)
    diffuse = np.clip(normal @ light, 0, 1)
    shade = 0.45 + 0.55 * diffuse
    shade = np.where(inside, shade, 1.0)[..., None]

    rgba = np.concatenate([rgb * shade, inside.astype(float)[..., None]], axis=-1)

    fig, ax = plt.subplots(figsize=(size_px / dpi, size_px / dpi), dpi=dpi)
    fig.patch.set_alpha(0)
    ax.set_facecolor("none")
    ax.imshow(rgba, extent=[-1, 1, -1, 1], origin="lower", interpolation="bilinear", zorder=0)
    ax.plot(np.cos(np.linspace(0, 2 * np.pi, 200)),
            np.sin(np.linspace(0, 2 * np.pi, 200)),
            color="#1f1f1f", linewidth=1.1, zorder=3)

    if pol_df is not None and len(pol_df) and "dx_km" in pol_df.columns:
        az = np.arctan2(pol_df["dx_km"].values, pol_df["dy_km"].values)
        # Simplified equal-area projection at 45° takeoff (no takeoff info)
        r = 0.7
        px = r * np.sin(az)
        py = r * np.cos(az)
        up   = pol_df["polarity"].values > 0
        down = pol_df["polarity"].values < 0
        if up.any():
            ax.scatter(px[up], py[up], marker="+", s=60, linewidths=1.1,
                       c="k", zorder=4)
        if down.any():
            ax.scatter(px[down], py[down], marker="o", s=45, linewidths=0.8,
                       edgecolor="k", facecolor="none", zorder=4)

    ax.set_xlim(-1.05, 1.05)
    ax.set_ylim(-1.05, 1.05)
    ax.set_aspect(1)
    ax.axis("off")
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    fig.savefig(out_path, dpi=dpi, transparent=True)
    plt.close(fig)


def regenerate_event_beachballs(out_csv, out_dir, pol_csv=None):
    """Regenerate one red/white shaded-sphere PNG per event (best-quality
    solution, same selection rule as MechanismModule._pickBestPerEvent in the
    GUI) from an existing SKHASH OUT/out.csv - does not need SKHASH to rerun,
    only the already-computed strike/dip/rake. Returns the number written."""
    import pandas as pd
    import os

    df = pd.read_csv(out_csv)
    if not len(df):
        return 0
    rank = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}
    df["_rank"] = df["quality"].map(rank).fillna(9)
    best = (df.sort_values(["_rank", "prob_mech"], ascending=[True, False])
              .groupby("event_id", as_index=False).first())

    pol_df = pd.read_csv(pol_csv) if pol_csv and os.path.exists(pol_csv) else None

    os.makedirs(out_dir, exist_ok=True)
    n = 0
    for _, row in best.iterrows():
        sub_pol = None
        if pol_df is not None and "takeoff" in pol_df.columns:
            sub_pol = pol_df[pol_df["event_id"].astype(str) == str(row["event_id"])]
            if not len(sub_pol) or sub_pol["takeoff"].isna().all():
                sub_pol = None
        out_path = os.path.join(out_dir, f"{row['event_id']}.png")
        plot_beachball_3d(row["strike"], row["dip"], row["rake"], out_path, pol_df=sub_pol)
        n += 1
    return n

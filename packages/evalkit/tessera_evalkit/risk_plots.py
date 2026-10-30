"""Reliability plot for the late-payment scorer (static PNG, light surface).

Colours are the first three validated categorical slots (blue, orange, aqua; all-pairs CVD
separation passes). Aqua is below 3:1 contrast on the light surface, so the relief rule applies:
a visible legend names every line, each has its own marker and dash style, and the same numbers
are written to ``late_reliability.csv``. (End-of-line labels were tried and dropped: the three
curves converge near (1, 1) and the labels collide.)
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .risk_metrics import reliability_table  # noqa: E402

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES = {  # name -> (colour, marker, dash, label)
    "p_logreg": ("#2a78d6", "o", "-", "Logistic regression, calibrated"),
    "p_lgbm_raw": ("#eb6834", "s", (0, (4, 2)), "LightGBM, uncalibrated"),
    "p_lgbm": ("#1baf7a", "D", "-", "LightGBM, calibrated"),
}


def reliability_frame(preds: pd.DataFrame, n_bins: int = 10) -> pd.DataFrame:
    parts = []
    for col in SERIES:
        t = reliability_table(preds["y"], preds[col], n_bins)
        t.insert(0, "model", col)
        parts.append(t)
    return pd.concat(parts, ignore_index=True)


def plot_reliability(preds: pd.DataFrame, path: Path, n_bins: int = 10, subtitle: str = "") -> pd.DataFrame:
    table = reliability_frame(preds, n_bins)
    fig, (ax, hx) = plt.subplots(1, 2, figsize=(10.5, 4.8), gridspec_kw={"width_ratios": [3, 2]}, facecolor=SURFACE)
    for a in (ax, hx):
        a.set_facecolor(SURFACE)
        for side in ("top", "right"):
            a.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            a.spines[side].set_color(GRID)
        a.tick_params(colors=INK2, labelsize=9)
        a.grid(color=GRID, linewidth=0.8)
        a.set_axisbelow(True)

    ax.plot([0, 1], [0, 1], color=INK2, linewidth=1, linestyle=":", zorder=1, label="Perfect calibration")
    for col, (colour, marker, dash, label) in SERIES.items():
        t = table[table["model"] == col]
        ax.plot(
            t["mean_pred"],
            t["frac_pos"],
            color=colour,
            linewidth=2,
            linestyle=dash,
            marker=marker,
            markersize=7,
            markeredgecolor=SURFACE,
            markeredgewidth=1.5,
            label=label,
            zorder=3,
        )
    ax.set_xlim(0, 1.02)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("Predicted P(late)", color=INK2)
    ax.set_ylabel("Observed share paid late", color=INK2)
    ax.set_title("Reliability on held-out test snapshots", loc="left", color=INK, fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", frameon=False, fontsize=8.5, labelcolor=INK)

    edges = np.linspace(0, 1, n_bins + 1)
    hx.hist(preds["p_lgbm"], bins=edges, color="#1baf7a", edgecolor=SURFACE, linewidth=2)
    hx.set_xlabel("Predicted P(late), LightGBM calibrated", color=INK2)
    hx.set_ylabel("Snapshots", color=INK2)
    hx.set_title("Where the predictions sit", loc="left", color=INK, fontsize=11, fontweight="bold")
    if subtitle:
        fig.text(0.01, 0.01, subtitle, color=INK2, fontsize=8.5)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return table

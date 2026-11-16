"""Plots for docs/eval-report.md. Deterministic: fixed palette, fixed sizes, no timestamps in the files.

Palette: the first three categorical slots of the reference palette (blue, orange, aqua), in fixed order, each also
given its own marker so identity never rests on colour alone; text in ink colours, never series colours.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES = {
    "lightgbm_conformal": ("#2a78d6", "o"),
    "seasonal_naive_conformal": ("#eb6834", "s"),
    "naive_conformal": ("#1baf7a", "^"),
}
LABEL = {
    "lightgbm_conformal": "LightGBM + conformal",
    "seasonal_naive_conformal": "Seasonal-naive + conformal",
    "naive_conformal": "Naive + conformal",
}
DPI = 140


def _style(ax: Axes) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.title.set_color(INK)
    ax.xaxis.label.set_color(MUTED)
    ax.yaxis.label.set_color(MUTED)


def _fig(ncols: int, w: float, h: float) -> tuple[Figure, list[Axes]]:
    fig, axes = plt.subplots(1, ncols, figsize=(w, h), facecolor=SURFACE)
    axes_list = [axes] if ncols == 1 else list(axes)
    for ax in axes_list:
        _style(ax)
    return fig, axes_list


def _save(fig: Figure, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=DPI, facecolor=SURFACE, metadata={"Software": "tessera_evalkit"})
    plt.close(fig)


def forecast_overview(summary: pd.DataFrame, path: Path, target: float) -> None:
    """WAPE and interval coverage by horizon, macro-average over currencies, with 95% business-bootstrap intervals."""
    all_rows = summary[summary["currency"] == "ALL"]
    fig, (a1, a2) = _fig(2, 10, 4)
    horizons = sorted(all_rows["horizon"].unique())
    for i, (model, (colour, marker)) in enumerate(SERIES.items()):
        g = all_rows[all_rows["model"] == model].sort_values("horizon")
        x = np.arange(len(horizons)) + (i - 1) * 0.18
        for ax, col in ((a1, "wape"), (a2, "coverage")):
            ax.errorbar(
                x,
                g[col],
                yerr=[g[col] - g[f"{col}_lo"], g[f"{col}_hi"] - g[col]],
                fmt=marker,
                color=colour,
                ecolor=colour,
                elinewidth=1.4,
                capsize=3,
                markersize=7,
                markeredgecolor=SURFACE,
                label=LABEL[model],
            )
    a2.axhline(target, color=MUTED, linestyle="--", linewidth=1.2, label=f"nominal {target:.0%}")
    for ax, title, yl in (
        (a1, "WAPE (lower is better)", "WAPE"),
        (a2, "Interval coverage", "share of outcomes inside the interval"),
    ):
        ax.set_xticks(range(len(horizons)), [f"{h} weeks" for h in horizons])
        ax.set_title(title, loc="left", fontsize=11)
        ax.set_ylabel(yl, fontsize=9)
    a1.set_yscale("log")
    a1.legend(frameon=False, fontsize=8, labelcolor=INK)
    a2.legend(frameon=False, fontsize=8, labelcolor=INK, loc="lower right")
    _save(fig, path)


def forecast_by_currency(summary: pd.DataFrame, path: Path, target: float) -> None:
    g = summary[(summary["model"] == "lightgbm_conformal") & (summary["currency"] != "ALL")]
    currencies = sorted(g["currency"].unique())
    fig, (a1, a2) = _fig(2, 10, 4)
    markers = {4: ("#2a78d6", "o"), 8: ("#eb6834", "s"), 13: ("#1baf7a", "^")}
    for h, (colour, marker) in markers.items():
        s = g[g["horizon"] == h].set_index("currency").reindex(currencies)
        x = np.arange(len(currencies)) + {4: -0.18, 8: 0.0, 13: 0.18}[h]
        for ax, col in ((a1, "wape"), (a2, "coverage")):
            ax.errorbar(
                x,
                s[col],
                yerr=[s[col] - s[f"{col}_lo"], s[f"{col}_hi"] - s[col]],
                fmt=marker,
                color=colour,
                ecolor=colour,
                elinewidth=1.4,
                capsize=3,
                markersize=6,
                markeredgecolor=SURFACE,
                label=f"{h} weeks",
            )
    a2.axhline(target, color=MUTED, linestyle="--", linewidth=1.2, label="nominal")
    for ax, title in ((a1, "LightGBM WAPE by currency"), (a2, "LightGBM coverage by currency")):
        ax.set_xticks(range(len(currencies)), currencies)
        ax.set_title(title, loc="left", fontsize=11)
        ax.legend(frameon=False, fontsize=8, labelcolor=INK)
    _save(fig, path)


def savings_overview(headline: pd.DataFrame, df: pd.DataFrame, path: Path) -> None:
    fig, (a1, a2) = _fig(2, 10.5, 4.2)
    colours = ["#2a78d6", "#eb6834"]
    for i, r in enumerate(headline.itertuples()):
        y = 1 - i
        a1.plot([r.lo, r.hi], [y + 0.08, y + 0.08], color=colours[i], linewidth=2, solid_capstyle="round")
        a1.plot(
            [r.week_lo, r.week_hi],
            [y - 0.08, y - 0.08],
            color=colours[i],
            linewidth=2,
            alpha=0.45,
            solid_capstyle="round",
        )
        a1.plot([r.saving_per_10k], [y], "o", color=colours[i], markersize=8, markeredgecolor=SURFACE)
    a1.axvline(0, color=MUTED, linewidth=1)
    a1.set_yticks([1, 0], [h.replace("convert on ", "vs convert on\n") for h in headline["baseline"]], fontsize=9)
    a1.set_ylim(-0.6, 1.6)
    a1.set_xlabel(
        "saving per 10,000 units converted (EUR); dark = obligation bootstrap, light = due-week clusters", fontsize=8
    )
    a1.set_title("Mean saving with 95% intervals", loc="left", fontsize=11)
    a1.grid(axis="x", color=GRID, linewidth=0.8)
    a1.grid(axis="y", visible=False)
    d = df.sort_values("due")
    for col, label, colour, marker in (
        ("save_vs_due_eur", "vs convert on due date", "#2a78d6", "o"),
        ("save_vs_invoice_eur", "vs convert on invoice date", "#eb6834", "s"),
    ):
        a2.plot(
            d["due"],
            d[col].cumsum(),
            color=colour,
            linewidth=1.8,
            label=label,
            marker=marker,
            markevery=max(1, len(d) // 8),
            markersize=5,
            markeredgecolor=SURFACE,
        )
    a2.axhline(0, color=MUTED, linewidth=1)
    a2.set_ylabel("cumulative saving (EUR)", fontsize=9)
    a2.set_title("Cumulative saving, in order of due date", loc="left", fontsize=11)
    a2.legend(frameon=False, fontsize=8, labelcolor=INK)
    a2.tick_params(axis="x", labelrotation=30)
    _save(fig, path)


def ablation_overview(rows: pd.DataFrame, path: Path, title_suffix: str = "") -> None:
    fig, (a1, a2) = _fig(2, 10.5, 4.2)
    names = list(rows["variant"])
    x = np.arange(len(names))
    bottoms = np.zeros(len(names))
    for col, label, colour in (
        ("approved", "approved", "#2a78d6"),
        ("escalated", "escalated", "#eb6834"),
        ("blocked", "blocked", "#1baf7a"),
    ):
        v = rows[col].to_numpy(dtype=float)
        a1.bar(x, v, bottom=bottoms, color=colour, width=0.55, label=label, edgecolor=SURFACE, linewidth=2)
        bottoms += v
    a1.set_xticks(x, [n.replace("without ", "w/o ").replace(" gate", "\ngate") for n in names], fontsize=7.5)
    a1.set_ylabel("proposals", fontsize=9)
    a1.set_title(f"Policy verdicts{title_suffix}", loc="left", fontsize=11)
    a1.legend(frameon=False, fontsize=8, labelcolor=INK)
    a2.errorbar(
        x,
        rows["saving_per_10k"],
        yerr=[rows["saving_per_10k"] - rows["lo"], rows["hi"] - rows["saving_per_10k"]],
        fmt="o",
        color="#2a78d6",
        ecolor="#2a78d6",
        elinewidth=1.4,
        capsize=3,
        markersize=7,
        markeredgecolor=SURFACE,
        label="saving vs due date (95%)",
    )
    a2.plot(
        x,
        rows["worst_obligation_saving_per_10k"],
        "v",
        color="#eb6834",
        markersize=7,
        markeredgecolor=SURFACE,
        label="worst single obligation",
    )
    a2.axhline(0, color=MUTED, linewidth=1)
    a2.set_xticks(x, [n.replace("without ", "w/o ").replace(" gate", "\ngate") for n in names], fontsize=7.5)
    a2.set_ylabel("per 10,000 units converted (EUR)", fontsize=9)
    a2.set_title(f"Savings and worst case{title_suffix}", loc="left", fontsize=11)
    a2.legend(frameon=False, fontsize=8, labelcolor=INK)
    _save(fig, path)

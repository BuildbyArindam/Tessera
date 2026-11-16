"""Bootstrap helpers for the Day 17 evaluation. All randomness comes from a seeded Generator the caller owns.

Two kinds of resampling are used, and the report says which:
- cluster bootstrap over BUSINESSES for forecast metrics (rows of one business are not independent);
- paired bootstrap over OBLIGATIONS (or over weekly clusters) for savings, where each unit carries a numerator
  (saving) and a denominator (notional) so the statistic is a ratio of sums, not a mean of ratios.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]
Interval = tuple[float, float]


def cluster_weights(n_clusters: int, n_boot: int, rng: np.random.Generator) -> FloatArray:
    """(n_boot, n_clusters) counts: how often each cluster is drawn in each replicate (multinomial)."""
    if n_clusters < 1:
        raise ValueError("need at least one cluster")
    return rng.multinomial(n_clusters, np.full(n_clusters, 1.0 / n_clusters), size=n_boot).astype(np.float64)


def percentile_interval(values: FloatArray, level: float = 0.95) -> Interval:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return (float("nan"), float("nan"))
    a = (1.0 - level) / 2.0
    lo, hi = np.quantile(finite, [a, 1.0 - a])
    return float(lo), float(hi)


def ratio_of_sums(num: FloatArray, den: FloatArray) -> float:
    d = float(den.sum())
    return float("nan") if d == 0.0 else float(num.sum()) / d


def bootstrap_ratio(
    num: FloatArray,
    den: FloatArray,
    weights: FloatArray,
    scale: float = 1.0,
    level: float = 0.95,
) -> tuple[float, Interval]:
    """Point estimate and percentile interval of scale * sum(num) / sum(den) over cluster resamples.

    `num` and `den` are per-cluster sums. `weights` come from `cluster_weights`, so several statistics can share
    the same resamples (a paired comparison is just two ratios evaluated on identical weights).
    """
    point = scale * ratio_of_sums(num, den)
    with np.errstate(divide="ignore", invalid="ignore"):
        draws = scale * (weights @ num) / (weights @ den)
    return point, percentile_interval(draws, level)


def paired_difference(
    num_a: FloatArray, den_a: FloatArray, num_b: FloatArray, den_b: FloatArray, weights: FloatArray, level: float = 0.95
) -> tuple[float, Interval]:
    """Ratio_a - ratio_b on identical resamples (the interval is for the DIFFERENCE, not two separate intervals)."""
    point = ratio_of_sums(num_a, den_a) - ratio_of_sums(num_b, den_b)
    with np.errstate(divide="ignore", invalid="ignore"):
        draws = (weights @ num_a) / (weights @ den_a) - (weights @ num_b) / (weights @ den_b)
    return point, percentile_interval(draws, level)


def group_sums(labels: NDArray[np.str_] | list[str], *columns: FloatArray) -> tuple[list[str], FloatArray]:
    """Per-cluster sums: returns (sorted cluster labels, array of shape (n_clusters, n_columns))."""
    lab = np.asarray(labels).astype(str)
    names = sorted(set(lab.tolist()))
    out = np.zeros((len(names), len(columns)), dtype=np.float64)
    for j, col in enumerate(columns):
        c = np.asarray(col, dtype=np.float64)
        for i, name in enumerate(names):
            out[i, j] = c[lab == name].sum()
    return names, out


def verdict(interval: Interval) -> str:
    """Plain words for a saving/difference interval where positive is good."""
    lo, hi = interval
    if not (np.isfinite(lo) and np.isfinite(hi)):
        return "undetermined"
    if lo > 0:
        return "positive (interval above zero)"
    if hi < 0:
        return "negative (interval below zero)"
    return "not distinguishable from zero (interval includes zero)"

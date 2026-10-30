"""Metrics for the Day 5 risk components.

Probabilistic classifier: AUC (ranking), Brier score (accuracy of the probability), and expected
calibration error (ECE: how far stated probabilities sit from observed frequencies). They answer
different questions: a model can rank well (high AUC) and still state probabilities that are too
confident, which is exactly what isotonic calibration is meant to fix.

Quantile forecasts: coverage per level = share of outcomes at or below the predicted quantile;
a calibrated 25% quantile should have ~25% of outcomes below it.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pandas as pd

Array = npt.ArrayLike


def _binary(y: Array, p: Array) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    yy = np.asarray(y, dtype=float)
    pp = np.asarray(p, dtype=float)
    if yy.shape != pp.shape or yy.ndim != 1:
        raise ValueError("y and p must be 1-D arrays of equal length")
    if yy.size == 0:
        raise ValueError("empty input")
    if not set(np.unique(yy)).issubset({0.0, 1.0}):
        raise ValueError("y must contain only 0 and 1")
    if not np.isfinite(pp).all() or (pp < 0).any() or (pp > 1).any():
        raise ValueError("p must be finite probabilities in [0, 1]")
    return yy, pp


def auc(y: Array, p: Array) -> float:
    """Area under the ROC curve via ranks (ties share the average rank). NaN if one class only."""
    yy, pp = _binary(y, p)
    n_pos = float(yy.sum())
    n_neg = float(len(yy) - n_pos)
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = pd.Series(pp).rank(method="average").to_numpy()
    return float((ranks[yy == 1].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def brier(y: Array, p: Array) -> float:
    """Mean squared error of the probability. 0 is perfect; predicting the base rate gives p(1-p)."""
    yy, pp = _binary(y, p)
    return float(np.mean((pp - yy) ** 2))


def reliability_table(y: Array, p: Array, n_bins: int = 10) -> pd.DataFrame:
    """Equal-width probability bins: count, mean predicted, observed frequency. Empty bins omitted."""
    if n_bins < 2:
        raise ValueError("n_bins must be >= 2")
    yy, pp = _binary(y, p)
    idx = np.minimum((pp * n_bins).astype(int), n_bins - 1)  # p == 1.0 falls in the last bin
    rows = []
    for b in range(n_bins):
        m = idx == b
        if m.any():
            rows.append(
                {
                    "bin_lo": b / n_bins,
                    "bin_hi": (b + 1) / n_bins,
                    "n": int(m.sum()),
                    "mean_pred": float(pp[m].mean()),
                    "frac_pos": float(yy[m].mean()),
                }
            )
    return pd.DataFrame(rows, columns=["bin_lo", "bin_hi", "n", "mean_pred", "frac_pos"])


def expected_calibration_error(y: Array, p: Array, n_bins: int = 10) -> float:
    """Count-weighted mean |observed frequency - mean predicted| over probability bins."""
    t = reliability_table(y, p, n_bins)
    return float((t["n"] * (t["frac_pos"] - t["mean_pred"]).abs()).sum() / t["n"].sum())


def classifier_report(y: Array, p: Array, n_bins: int = 10) -> dict[str, float]:
    yy, pp = _binary(y, p)
    return {
        "n": float(len(yy)),
        "base_rate": float(yy.mean()),
        "auc": auc(yy, pp),
        "brier": brier(yy, pp),
        "ece": expected_calibration_error(yy, pp, n_bins),
    }


def quantile_coverage(realized: Array, predicted: npt.NDArray[np.float64], levels: tuple[int, ...]) -> pd.DataFrame:
    """Observed share of outcomes <= each predicted quantile.

    ``predicted`` has shape (n, len(levels)). ``se`` is the binomial standard error treating the
    n rows as independent; with overlapping horizons it is optimistic, so callers that know the
    effective sample size should pass their own.
    """
    r = np.asarray(realized, dtype=float)
    q = np.asarray(predicted, dtype=float)
    if q.ndim != 2 or q.shape != (len(r), len(levels)):
        raise ValueError(f"predicted must have shape ({len(r)}, {len(levels)})")
    if len(r) == 0:
        raise ValueError("empty input")
    rows = []
    for j, lv in enumerate(levels):
        nominal = lv / 100.0
        observed = float(np.mean(r <= q[:, j]))
        rows.append(
            {
                "level": lv,
                "nominal": nominal,
                "observed": observed,
                "gap": observed - nominal,
                "se": float(np.sqrt(nominal * (1 - nominal) / len(r))),
            }
        )
    return pd.DataFrame(rows)

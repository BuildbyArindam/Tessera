"""Point-forecast metrics.

Why not plain MAPE
------------------
MAPE = mean(|y - yhat| / |y|). Net weekly cash flow crosses zero: inflows and
outflows cancel, so many actuals are tiny or exactly zero. Then:

* y == 0 makes a term undefined (division by zero);
* y ~ 1 with an error of 500 gives a 50,000% term that swamps every other
  observation, so the average reflects one near-zero week, not model quality;
* it is asymmetric: over-forecasts are unbounded, under-forecasts cap at 100%.

WAPE = sum|y - yhat| / sum|y| divides by the *total* magnitude instead, so one
near-zero week cannot dominate. It is our primary metric. sMAPE is reported as
a secondary view; its denominator can still be small, so terms with
|y| + |yhat| == 0 are scored 0 (a perfect forecast of zero).

Sign convention: bias = mean(yhat - y); positive means over-forecasting.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

Array = npt.ArrayLike


def _prep(y_true: Array, y_pred: Array) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(y_pred, dtype=float)
    if y.shape != p.shape:
        raise ValueError(f"shape mismatch: {y.shape} vs {p.shape}")
    if y.size == 0:
        raise ValueError("empty input")
    return y, p


def wape(y_true: Array, y_pred: Array) -> float:
    """sum|y - yhat| / sum|y|. NaN if every actual is zero (undefined)."""
    y, p = _prep(y_true, y_pred)
    denom = float(np.abs(y).sum())
    return float("nan") if denom == 0.0 else float(np.abs(y - p).sum() / denom)


def smape(y_true: Array, y_pred: Array) -> float:
    """mean(2|y - yhat| / (|y| + |yhat|)); 0/0 terms count as 0. Range [0, 2]."""
    y, p = _prep(y_true, y_pred)
    denom = np.abs(y) + np.abs(p)
    terms = np.divide(2.0 * np.abs(y - p), denom, out=np.zeros_like(denom), where=denom != 0)
    return float(terms.mean())


def mae(y_true: Array, y_pred: Array) -> float:
    y, p = _prep(y_true, y_pred)
    return float(np.abs(y - p).mean())


def bias(y_true: Array, y_pred: Array) -> float:
    """mean(yhat - y). Positive = over-forecast."""
    y, p = _prep(y_true, y_pred)
    return float((p - y).mean())

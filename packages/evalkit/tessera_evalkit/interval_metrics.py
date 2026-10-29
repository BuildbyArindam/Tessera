"""Point and interval metrics.

WAPE = sum|y - yhat| / sum|y|. Preferred over MAPE because weekly NET cash flow crosses
zero: MAPE divides each error by its own |y|, so near-zero weeks explode the average
(and exact zeros are undefined). WAPE weights errors by total volume instead.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]


def wape(y: FloatArray, yhat: FloatArray) -> float:
    denom = float(np.sum(np.abs(y)))
    return float("nan") if denom == 0.0 else float(np.sum(np.abs(y - yhat)) / denom)


def coverage(y: FloatArray, lower: FloatArray, upper: FloatArray) -> float:
    return float(np.mean((y >= lower) & (y <= upper)))


def mean_width(lower: FloatArray, upper: FloatArray) -> float:
    return float(np.mean(upper - lower))


def pinball(y: FloatArray, q: FloatArray, tau: float) -> float:
    """Mean pinball (quantile) loss of predicted quantile q at level tau."""
    d = y - q
    return float(np.mean(np.maximum(tau * d, (tau - 1.0) * d)))


def interval_pinball(y: FloatArray, lower: FloatArray, upper: FloatArray, target_coverage: float) -> float:
    """Average pinball loss of the two interval endpoints (levels (1-c)/2 and 1-(1-c)/2)."""
    a = (1.0 - target_coverage) / 2.0
    return 0.5 * (pinball(y, lower, a) + pinball(y, upper, 1.0 - a))

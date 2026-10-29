"""Baseline forecasters.

Forecast target: cumulative net cash flow over the next ``h`` weeks
(h = 4, 8, 13 are roughly the 30/60/90-day windows the product shows).
Every baseline sees only the weekly history up to the origin, as a Series.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

import numpy as np
import pandas as pd

Baseline = Callable[[pd.Series, int], float]

SEASON_WEEKS = 52
SHORT_HISTORY_WINDOW = 4
MOVING_AVERAGE_WINDOW = 8


def naive(y: pd.Series, h: int) -> float:
    """Repeat the last observed week for each of the next h weeks."""
    return float(y.iloc[-1]) * h


def moving_average(y: pd.Series, h: int, window: int = MOVING_AVERAGE_WINDOW) -> float:
    """Mean of the last ``window`` weeks, repeated h times."""
    return float(np.mean(y.to_numpy(dtype=float)[-window:])) * h


def seasonal_naive(y: pd.Series, h: int, season: int = SEASON_WEEKS) -> float:
    """Sum of the same h weeks one year earlier.

    Falls back to the 4-week average (x h) when there is less than one season
    of history. 52 weeks = 364 days, so the alignment drifts by about a day a
    year; that is acceptable for a baseline.
    """
    values = y.to_numpy(dtype=float)
    n = len(values)
    if n < season:
        return float(np.mean(values[-SHORT_HISTORY_WINDOW:])) * h
    # Next weeks are positions n .. n+h-1; one season earlier: n-season .. n-season+h-1.
    return float(values[n - season : n - season + h].sum())


BASELINES: dict[str, Baseline] = {
    "naive": naive,
    "seasonal_naive": seasonal_naive,
    "moving_average": partial(moving_average),
}

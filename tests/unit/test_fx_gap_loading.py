"""A hole in loaded FX data must be a typed error; a holiday must not be."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from tessera_evalkit.fx_backtest import MAX_MISSING_BDAYS, FxDataGapError, series_from_frame


def _series(n: int = 300) -> pd.Series:
    idx = pd.bdate_range("2024-01-01", periods=n)
    return pd.Series(83 * np.exp(np.cumsum(np.random.default_rng(0).normal(0, 0.004, n))), index=idx, name="USDINR")


def test_holiday_sized_holes_are_tolerated_and_nan_rows_are_dropped() -> None:
    s = _series()
    assert len(series_from_frame(s.drop(s.index[50 : 50 + MAX_MISSING_BDAYS]))) == len(s) - MAX_MISSING_BDAYS
    with_nan = s.copy()
    with_nan.iloc[10] = np.nan
    assert len(series_from_frame(with_nan)) == len(s) - 1


def test_long_hole_raises_with_the_dates() -> None:
    s = _series()
    with pytest.raises(FxDataGapError, match="business days missing between"):
        series_from_frame(s.drop(s.index[100 : 100 + MAX_MISSING_BDAYS + 1]))
    with pytest.raises(FxDataGapError, match="USDINR"):  # a long run of NaN is the same hole
        gap = s.copy()
        gap.iloc[100:120] = np.nan
        series_from_frame(gap)

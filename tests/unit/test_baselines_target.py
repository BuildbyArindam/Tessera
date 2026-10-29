import numpy as np
import pandas as pd
import pytest

from tessera_core.forecast.baselines import moving_average, naive, seasonal_naive
from tessera_core.forecast.target import last_complete_week_start, weekly_net_cashflow


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range("2025-01-06", periods=len(values), freq="7D")
    return pd.Series(values, index=idx)


def test_naive() -> None:
    assert naive(_series([1, 2, 5]), 4) == 20.0


def test_moving_average_uses_last_window() -> None:
    y = _series([100.0] * 10 + [10.0] * 8)
    assert moving_average(y, 4) == pytest.approx(40.0)


def test_seasonal_naive_short_history_falls_back_to_4wk_mean() -> None:
    y = _series([0, 0, 0, 10, 20, 30, 40])
    assert seasonal_naive(y, 4) == pytest.approx(25.0 * 4)


def test_seasonal_naive_uses_same_weeks_last_year() -> None:
    y = _series([float(i) for i in range(60)])  # n=60 => next weeks map to positions 8..11
    assert seasonal_naive(y, 4) == pytest.approx(8 + 9 + 10 + 11)


def test_weekly_target_zero_fills_and_nets_flows() -> None:
    cash = pd.DataFrame(
        {
            "business_id": ["b"] * 3,
            "currency": ["USD"] * 3,
            "cash_date": pd.to_datetime(["2025-01-07", "2025-01-08", "2025-01-22"]),
            "amount": [100.0, -30.0, 50.0],
        }
    )
    w = weekly_net_cashflow(cash)
    assert list(w["net"]) == [70.0, 0.0, 50.0]  # gap week is zero, not missing


def test_partial_current_week_is_excluded() -> None:
    cash = pd.DataFrame(
        {
            "business_id": ["b"] * 2,
            "currency": ["USD"] * 2,
            "cash_date": pd.to_datetime(["2025-01-07", "2025-01-15"]),
            "amount": [100.0, 999.0],
        }
    )
    # Wed 2025-01-15: week of Jan 13 is incomplete
    w = weekly_net_cashflow(cash, as_of=pd.Timestamp("2025-01-15"))
    assert list(w["net"]) == [100.0]
    assert last_complete_week_start(pd.Timestamp("2025-01-12")) == pd.Timestamp("2025-01-06")  # Sunday
    assert not np.isnan(w["net"]).any()

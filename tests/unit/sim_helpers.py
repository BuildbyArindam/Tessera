"""Builders for the Day 8 simulation tests: synthetic FX series and the three simulated proposals."""

from datetime import date, timedelta

import numpy as np
from policy_helpers import make_proposal
from tessera_core.domain import ActionType, Proposal
from tessera_core.simulate import FxSeries

START = date(2023, 1, 2)
PAIR = "USD/EUR"


def business_days(n: int, start: date = START) -> list[date]:
    days: list[date] = []
    d = start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def make_series(
    n: int = 800, daily_vol: float = 0.005, seed: int = 1, level: float = 0.92, pair: str = PAIR, drift: float = 0.0
) -> FxSeries:
    """Random-walk rates with the given daily volatility and drift (0 vol gives a constant series)."""
    steps = np.random.default_rng(seed).normal(drift, daily_vol, n)
    values = level * np.exp(np.cumsum(steps))
    return FxSeries.from_observations(pair, list(zip(business_days(n), values.tolist(), strict=True)))


def constant_series(n: int = 200, level: float = 0.9, pair: str = PAIR) -> FxSeries:
    return make_series(n=n, daily_vol=0.0, level=level, pair=pair)


def rebuilt(series: FxSeries, values: np.ndarray) -> FxSeries:
    return FxSeries(series.pair, series.days.copy(), np.asarray(values, dtype=np.float64))


def scaled_after(series: FxSeries, as_of: date, factor: float) -> FxSeries:
    """Same series, but every observation dated after `as_of` multiplied by `factor`."""
    later = series.days > np.datetime64(as_of, "D")
    return rebuilt(series, np.where(later, series.values * factor, series.values))


def convert(amount: str = "10000.00", sell: str = "USD", buy: str = "EUR") -> Proposal:
    return make_proposal(ActionType.CONVERT, amount, sell, to_currency=buy)


def hold(amount: str = "10000.00", sell: str = "USD", buy: str = "EUR") -> Proposal:
    return make_proposal(ActionType.HOLD, amount, sell, to_currency=buy)


def pay(amount: str = "9000.00", payout: str = "EUR", funded_from: str | None = "USD") -> Proposal:
    extra = {} if funded_from is None else {"from_currency": funded_from}
    return make_proposal(ActionType.PAY_SUPPLIER, amount, payout, beneficiary_id="sup_old", **extra)

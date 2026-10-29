"""Rolling-origin (expanding window) backtest.

An *origin* is a Sunday evening: weeks up to it are train, the following
``h`` weeks are test. Origins advance chronologically by ``step_weeks`` and
the window expands (train always starts at the series' first week).

All horizons share the same origins so their errors are comparable. At each
origin the forecasters are handed only ``history.as_of(origin)``; the future
is touched solely to compute ``y_true`` after the prediction is made.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

import pandas as pd

from tessera_core.forecast.baselines import Baseline
from tessera_core.forecast.history import History
from tessera_core.forecast.target import series_by_key, weekly_net_cashflow

DEFAULT_HORIZONS = (4, 8, 13)
RESULT_COLUMNS = ["business_id", "currency", "origin", "horizon", "model", "y_true", "y_pred", "n_train_weeks"]


@dataclass(frozen=True)
class Fold:
    origin: pd.Timestamp
    horizon: int
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp

    def validate(self) -> None:
        if not self.train_end < self.test_start:
            raise ValueError(f"fold leaks: train_end {self.train_end} >= test_start {self.test_start}")


def _origins(weekly_full: pd.DataFrame, horizons: Sequence[int], min_train_weeks: int, step_weeks: int) -> list[pd.Timestamp]:
    first = weekly_full["week_start"].min()
    last_week = weekly_full["week_start"].max()
    first_origin = first + pd.Timedelta(weeks=min_train_weeks) - pd.Timedelta(days=1)
    last_origin = last_week + pd.Timedelta(days=6) - pd.Timedelta(weeks=max(horizons))
    return list(pd.date_range(first_origin, last_origin, freq=f"{7 * step_weeks}D"))


def iter_folds(
    history: History,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    min_train_weeks: int = 26,
    step_weeks: int = 4,
) -> Iterator[Fold]:
    """Yield every (origin, horizon) fold, validated, in chronological order."""
    data_end = history.cashflows["cash_date"].max()
    weekly_full = weekly_net_cashflow(history.cashflows, as_of=data_end)
    for origin in _origins(weekly_full, horizons, min_train_weeks, step_weeks):
        for h in horizons:
            fold = Fold(
                origin=origin,
                horizon=h,
                train_end=origin,
                test_start=origin + pd.Timedelta(days=1),
                test_end=origin + pd.Timedelta(weeks=h),
            )
            fold.validate()
            yield fold


def rolling_origin_backtest(
    history: History,
    forecasters: Mapping[str, Baseline],
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    min_train_weeks: int = 26,
    step_weeks: int = 4,
) -> pd.DataFrame:
    """Long frame of predictions vs actuals (columns: ``RESULT_COLUMNS``).

    y_true and y_pred are the cumulative net cash flow over the next h weeks.
    """
    data_end = history.cashflows["cash_date"].max()
    full = series_by_key(weekly_net_cashflow(history.cashflows, as_of=data_end))
    origins = sorted({f.origin for f in iter_folds(history, horizons, min_train_weeks, step_weeks)})

    records: list[dict[str, object]] = []
    for origin in origins:
        known = history.as_of(origin)
        train = series_by_key(weekly_net_cashflow(known.cashflows, as_of=origin))
        for key, y_hist in train.items():
            if len(y_hist) < min_train_weeks:
                continue
            business_id, currency = key
            for h in horizons:
                fold = Fold(origin, h, origin, origin + pd.Timedelta(days=1), origin + pd.Timedelta(weeks=h))
                fold.validate()
                test_weeks = pd.date_range(fold.test_start, periods=h, freq="7D")
                actual = full[key].reindex(test_weeks)
                if actual.isna().any():
                    raise ValueError(f"test window for {key} at {origin} extends past the data")
                y_true = float(actual.sum())
                for name, fn in forecasters.items():
                    records.append(
                        {
                            "business_id": business_id,
                            "currency": currency,
                            "origin": origin,
                            "horizon": h,
                            "model": name,
                            "y_true": y_true,
                            "y_pred": float(fn(y_hist, h)),
                            "n_train_weeks": len(y_hist),
                        }
                    )
    return pd.DataFrame(records, columns=RESULT_COLUMNS)

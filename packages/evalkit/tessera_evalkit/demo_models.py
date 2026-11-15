"""Fit the Day 4 forecaster and the Day 5 late-payment scorer on the synthetic business, for the demo (Day 15).

The forecaster is trained on weekly origins (Sundays) with one label per horizon. Horizon h means: the NET cash flow of
the week that starts h weeks after the last complete week, as in the Day 4 contract. The dashboard shows horizons 1..13
so that 30 / 60 / 90 days are marks on a continuous weekly curve (4, 8 and 13 weeks: 28, 56 and 91 days).

Everything here is fitted ONLY on data up to ``data_end``; the live forecasts are the frame at ``data_end`` itself.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from tessera_core.forecast.direct_lgbm import DirectForecaster, target_col
from tessera_core.forecast.features import FEATURE_COLUMNS, build_features
from tessera_core.forecast.history import History
from tessera_core.forecast.results import ForecastResult
from tessera_core.forecast.target import series_by_key, weekly_net_cashflow
from tessera_core.risk import LatePaymentAssessment, LateScorer, assess_late_payment, build_snapshot_dataset, prepare
from tessera_core.risk.late_model import fit_late_scorer
from tessera_core.risk.late_split import chrono_split

from tessera_evalkit.loading import load_history
from tessera_evalkit.risk_data import load_invoice_table

HORIZONS = tuple(range(1, 14))
MIN_ORIGIN_WEEKS = 8  # skip origins with almost no history: their lags are nearly all NaN
CAL_WEEKS = 16
LATE_CAL_DAYS = 91
MODEL_FEATURES = [c for c in FEATURE_COLUMNS if c != "as_of"]


def sunday_origins(history: History, data_end: pd.Timestamp) -> list[pd.Timestamp]:
    first = history.cashflows["cash_date"].min().normalize()
    sundays = pd.date_range(first + pd.Timedelta(weeks=MIN_ORIGIN_WEEKS), data_end, freq="W-SUN")
    return list(sundays)


def build_training_frame(history: History, data_end: pd.Timestamp) -> pd.DataFrame:
    """One row per (business, currency, Sunday origin): features known at the origin plus y_h1 .. y_h13."""
    weekly = series_by_key(weekly_net_cashflow(history.cashflows, as_of=data_end))
    parts: list[pd.DataFrame] = []
    for origin in sunday_origins(history, data_end):
        feats = build_features(history, origin).reset_index()
        if feats.empty:
            continue
        first_label_week = origin + pd.Timedelta(days=1)
        for h in HORIZONS:
            week = first_label_week + pd.Timedelta(weeks=h - 1)
            feats[target_col(h)] = [
                float(weekly[(b, c)].get(week, np.nan)) if (b, c) in weekly else np.nan
                for b, c in zip(feats["business_id"], feats["currency"], strict=True)
            ]
        parts.append(feats)
    frame = pd.concat(parts, ignore_index=True)
    frame["as_of"] = pd.to_datetime(frame["as_of"])
    return frame


def fit_forecaster(frame: pd.DataFrame, data_end: pd.Timestamp, seed: int = 0) -> DirectForecaster:
    test_start = data_end + pd.Timedelta(days=1)  # nothing is held out: the live forecasts are made at data_end
    model = DirectForecaster(HORIZONS, MODEL_FEATURES, coverage=0.8, seed=seed)
    return model.fit(frame, test_start=test_start, cal_weeks=CAL_WEEKS)  # type: ignore[no-any-return]


def live_forecasts(model: DirectForecaster, history: History, data_end: pd.Timestamp) -> list[ForecastResult]:
    return model.forecast(build_features(history, data_end).reset_index())


def fit_late(data_dir: str | Path, seed: int = 0) -> tuple[LateScorer, pd.DataFrame, pd.Timestamp]:
    invoices, data_end = load_invoice_table(data_dir)
    ds = build_snapshot_dataset(prepare(invoices), data_end)
    split = chrono_split(
        ds["origin"],
        ds["label_known"],
        data_end - pd.Timedelta(days=LATE_CAL_DAYS),
        data_end,
        data_end + pd.Timedelta(days=1),
    )
    scorer = fit_late_scorer(ds.iloc[split.train_idx], ds.iloc[split.cal_idx], seed=seed)
    return scorer, invoices, data_end


def score_open_invoices(
    scorer: LateScorer, invoices: pd.DataFrame, data_end: pd.Timestamp
) -> list[LatePaymentAssessment]:
    return assess_late_payment(
        scorer, invoices, data_end.date() if isinstance(data_end, pd.Timestamp) else date.fromisoformat(str(data_end))
    )


def load_everything(data_dir: str | Path) -> tuple[History, pd.Timestamp]:
    _, data_end = load_invoice_table(data_dir)
    return load_history(data_dir), data_end

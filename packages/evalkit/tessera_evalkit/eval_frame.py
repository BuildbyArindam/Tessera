"""The Day 17 modelling frame: one row per (business, currency, Sunday origin).

Columns: the Day 3 features (built only from data on or before the origin, through History.as_of), the labels
`y_h{4,8,13}` (CUMULATIVE net cash flow over the next h weeks, the Day 3 target, NaN while not yet observable) and the
two baseline point forecasts `naive_h{h}` / `snaive_h{h}` computed from the series known at the origin.

The label uses the full history, which is correct for a label (it is what happened); everything else is as-of.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from tessera_core.forecast.baselines import naive, seasonal_naive
from tessera_core.forecast.direct_lgbm import target_col
from tessera_core.forecast.features import FEATURE_COLUMNS, build_features
from tessera_core.forecast.history import History
from tessera_core.forecast.target import series_by_key, weekly_net_cashflow

from tessera_evalkit.eval_config import HORIZONS

MIN_ORIGIN_WEEKS = 8  # the Day 15 choice: earlier origins have almost no history
MODEL_FEATURES = [c for c in FEATURE_COLUMNS if c != "as_of"]
BASELINE_COLUMNS = {"naive": "naive_h{h}", "seasonal_naive": "snaive_h{h}"}


def sunday_origins(history: History, data_end: pd.Timestamp) -> list[pd.Timestamp]:
    first = history.cashflows["cash_date"].min().normalize()
    return list(pd.date_range(first + pd.Timedelta(weeks=MIN_ORIGIN_WEEKS), data_end, freq="W-SUN"))


def build_eval_frame(history: History, data_end: pd.Timestamp, horizons: tuple[int, ...] = HORIZONS) -> pd.DataFrame:
    full = series_by_key(weekly_net_cashflow(history.cashflows, as_of=data_end))
    parts: list[pd.DataFrame] = []
    for origin in sunday_origins(history, data_end):
        known = history.as_of(origin)
        feats = build_features(history, origin).reset_index()
        if feats.empty:
            continue
        seen = series_by_key(weekly_net_cashflow(known.cashflows, as_of=origin))
        first_week = origin + pd.Timedelta(days=1)
        keys = list(zip(feats["business_id"], feats["currency"], strict=True))
        for h in horizons:
            weeks = pd.date_range(first_week, periods=h, freq="7D")
            labels = []
            for key in keys:
                vals = full[key].reindex(weeks) if key in full else pd.Series(np.nan, index=weeks)
                labels.append(float(vals.sum()) if not vals.isna().any() else float("nan"))
            feats[target_col(h)] = labels
            feats[f"naive_h{h}"] = [naive(seen[k], h) for k in keys]
            feats[f"snaive_h{h}"] = [seasonal_naive(seen[k], h) for k in keys]
        parts.append(feats)
    frame = pd.concat(parts, ignore_index=True)
    frame["as_of"] = pd.to_datetime(frame["as_of"])
    return frame.sort_values(["as_of", "business_id", "currency"]).reset_index(drop=True)

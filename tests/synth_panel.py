"""Tiny synthetic weekly cash-flow panel in the Day 4 input contract. Test-only."""

from __future__ import annotations

import numpy as np
import pandas as pd

HORIZONS = (4, 8, 13)
N_LAGS = 12
FEATURES = [f"lag_{k}" for k in range(1, N_LAGS + 1)] + [
    "roll_mean_4",
    "roll_std_4",
    "week_of_year",
    "month_end",
    "seasonal_lag_h{h}",
]


def concrete_features() -> list[str]:
    return [c.format(h=h) for c in FEATURES for h in HORIZONS if "{h}" in c] + [c for c in FEATURES if "{h}" not in c]


def make_panel(
    n_businesses: int = 10, n_weeks: int = 200, seed: int = 0
) -> tuple[pd.DataFrame, dict[tuple[str, str], np.ndarray]]:
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2021-01-04")
    series: dict[tuple[str, str], np.ndarray] = {}
    rows: list[pd.DataFrame] = []
    weeks = np.arange(n_weeks)
    for b in range(n_businesses):
        for ci, cur in enumerate(("USD", "EUR", "GBP")):
            level = rng.uniform(5_000, 40_000)
            season = 1 + 0.35 * np.sin(2 * np.pi * (weeks + rng.integers(0, 52)) / 52)
            trend = 1 + 0.002 * weeks
            ar = np.zeros(n_weeks)
            for t in range(1, n_weeks):
                ar[t] = 0.5 * ar[t - 1] + rng.normal(0, 0.12 * (1 + 0.3 * ci))
            spikes = (rng.random(n_weeks) < 0.03) * rng.uniform(0.5, 1.5, n_weeks)
            y = level * (season * trend + ar + spikes) - 0.4 * level
            series[(f"b{b}", cur)] = y
            as_of = start + pd.to_timedelta(weeks, unit="W")
            data: dict[str, object] = {"business_id": f"b{b}", "currency": cur, "as_of": as_of}
            for k in range(1, N_LAGS + 1):  # lag_1 = latest observed week (<= as_of)
                data[f"lag_{k}"] = pd.Series(y).shift(k - 1).to_numpy()
            s = pd.Series(y)
            data["roll_mean_4"] = s.rolling(4).mean().to_numpy()
            data["roll_std_4"] = s.rolling(4).std().to_numpy()
            data["week_of_year"] = as_of.isocalendar().week.to_numpy().astype(float)
            data["month_end"] = (as_of.day >= 22).astype(float)
            for h in HORIZONS:
                data[f"seasonal_lag_h{h}"] = s.shift(52 - h).to_numpy()  # y[t + h - 52]: observed by t
                data[f"y_h{h}"] = s.shift(-h).to_numpy()  # NaN near the end: not yet observable
            rows.append(pd.DataFrame(data))
    frame = pd.concat(rows, ignore_index=True)
    frame = (
        frame.dropna(subset=concrete_features())
        .sort_values(["as_of", "business_id", "currency"])
        .reset_index(drop=True)
    )
    return frame, series


def naive(frame: pd.DataFrame, h: int) -> np.ndarray:
    return frame["lag_1"].to_numpy(dtype="float64")


START = pd.Timestamp("2021-01-04")


def seasonal_naive_factory(series: dict[tuple[str, str], np.ndarray], start: pd.Timestamp = START):
    def fn(frame: pd.DataFrame, h: int) -> np.ndarray:
        out = np.empty(len(frame))
        for i, (b, c, t) in enumerate(zip(frame["business_id"], frame["currency"], frame["as_of"], strict=True)):
            wk = int((pd.Timestamp(t) - start).days // 7)
            idx = wk + h - 52  # same week last year; always <= wk for h <= 52 (no look-ahead)
            y = series[(b, c)]
            out[i] = y[idx] if idx >= 0 else float(np.mean(y[max(0, wk - 3) : wk + 1]))
        return out

    return fn

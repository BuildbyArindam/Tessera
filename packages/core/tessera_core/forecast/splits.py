"""Chronological train / calibration / test splits with a horizon-sized embargo.

A row is a forecast ORIGIN `as_of`; its label is realised at as_of + h weeks. A naive
split on origins leaks: a training row just before the cut has a label that lands inside
the calibration (or test) period. We therefore require every label used at a stage to be
realised BEFORE the next stage starts:

    train rows : as_of + h < cal_start
    cal rows   : cal_start <= as_of  and  as_of + h < test_start
    test rows  : test_start <= as_of < test_end

This is also what a live deployment sees: residuals used for calibration are known by the
time the test-period forecasts are issued.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from numpy.typing import NDArray

IntArray = NDArray[np.int64]


@dataclass(frozen=True)
class Split:
    horizon_weeks: int
    cal_start: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_idx: IntArray
    cal_idx: IntArray
    test_idx: IntArray


def label_time(as_of: pd.Series, horizon_weeks: int) -> pd.Series:
    return as_of + pd.Timedelta(weeks=horizon_weeks)


def make_split(
    as_of: pd.Series,
    horizon_weeks: int,
    test_start: pd.Timestamp,
    test_end: pd.Timestamp,
    cal_weeks: int,
) -> Split:
    """Positional index arrays (into `as_of`) for the three stages."""
    if cal_weeks <= 0:
        raise ValueError("cal_weeks must be positive")
    if test_end <= test_start:
        raise ValueError("test_end must be after test_start")
    cal_start = test_start - pd.Timedelta(weeks=cal_weeks)
    t = pd.to_datetime(as_of).reset_index(drop=True)
    lab = label_time(t, horizon_weeks)
    train = np.flatnonzero((lab < cal_start).to_numpy())
    cal = np.flatnonzero(((t >= cal_start) & (lab < test_start)).to_numpy())
    test = np.flatnonzero(((t >= test_start) & (t < test_end)).to_numpy())
    split = Split(
        horizon_weeks,
        cal_start,
        test_start,
        test_end,
        train.astype(np.int64),
        cal.astype(np.int64),
        test.astype(np.int64),
    )
    assert_split_clean(t, split)
    return split


def assert_split_clean(as_of: pd.Series, split: Split) -> None:
    """Raise AssertionError if train/cal/test overlap or are not strictly chronological."""
    h = split.horizon_weeks
    t = pd.to_datetime(as_of).reset_index(drop=True)
    sets = [set(split.train_idx.tolist()), set(split.cal_idx.tolist()), set(split.test_idx.tolist())]
    assert not (sets[0] & sets[1]), "train and calibration rows overlap"
    assert not (sets[0] & sets[2]), "train and test rows overlap"
    assert not (sets[1] & sets[2]), "calibration and test rows overlap"
    if split.train_idx.size:
        assert (t.iloc[split.train_idx] + pd.Timedelta(weeks=h)).max() < split.cal_start, (
            "a training label is realised inside the calibration window"
        )
    if split.cal_idx.size:
        assert t.iloc[split.cal_idx].min() >= split.cal_start
        assert (t.iloc[split.cal_idx] + pd.Timedelta(weeks=h)).max() < split.test_start, (
            "a calibration label is realised inside the test window"
        )
    if split.test_idx.size:
        assert t.iloc[split.test_idx].min() >= split.test_start


def rolling_test_windows(as_of: pd.Series, n_folds: int, test_frac: float) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Contiguous test windows over the last `test_frac` of origins (expanding-window folds)."""
    if n_folds <= 0 or not 0.0 < test_frac < 1.0:
        raise ValueError("need n_folds > 0 and 0 < test_frac < 1")
    dates = np.sort(pd.to_datetime(as_of).unique())
    first = int(len(dates) * (1.0 - test_frac))
    edges = np.linspace(first, len(dates), n_folds + 1).astype(int)
    last_excl = dates[-1] + np.timedelta64(1, "D")
    out: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for k in range(n_folds):
        start = pd.Timestamp(dates[edges[k]])
        end = pd.Timestamp(dates[edges[k + 1]]) if k < n_folds - 1 else pd.Timestamp(last_excl)
        out.append((start, end))
    return out

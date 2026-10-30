"""Chronological train / calibration / test split for snapshot rows, with a label embargo.

Each row is a snapshot at ``origin`` whose label becomes known at ``label_known``. Rule: a label
used at a stage must be known BEFORE the next stage begins.

    train : label_known <  cal_start
    cal   : origin >= cal_start  and  label_known < test_start
    test  : test_start <= origin < test_end

Without the embargo, a snapshot taken just before ``cal_start`` whose invoice is only paid inside
the calibration window would leak calibration-period outcomes into training.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd

IntArray = npt.NDArray[np.int64]


@dataclass(frozen=True)
class ChronoSplit:
    cal_start: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_idx: IntArray
    cal_idx: IntArray
    test_idx: IntArray


def chrono_split(
    origin: pd.Series,
    label_known: pd.Series,
    cal_start: pd.Timestamp,
    test_start: pd.Timestamp,
    test_end: pd.Timestamp,
) -> ChronoSplit:
    """Positional index arrays into ``origin``; raises ``ValueError`` on bad boundaries."""
    if not cal_start < test_start < test_end:
        raise ValueError("need cal_start < test_start < test_end")
    if len(origin) != len(label_known):
        raise ValueError("origin and label_known must have the same length")
    o = pd.to_datetime(origin).reset_index(drop=True)
    k = pd.to_datetime(label_known).reset_index(drop=True)
    train = np.flatnonzero((k < cal_start).to_numpy())
    cal = np.flatnonzero(((o >= cal_start) & (k < test_start)).to_numpy())
    test = np.flatnonzero(((o >= test_start) & (o < test_end)).to_numpy())
    split = ChronoSplit(
        cal_start, test_start, test_end, train.astype(np.int64), cal.astype(np.int64), test.astype(np.int64)
    )
    assert_clean(o, k, split)
    return split


def assert_clean(o: pd.Series, k: pd.Series, split: ChronoSplit) -> None:
    """Raise AssertionError if the stages overlap or any label crosses a stage boundary."""
    sets = [set(split.train_idx.tolist()), set(split.cal_idx.tolist()), set(split.test_idx.tolist())]
    assert not (sets[0] & sets[1]) and not (sets[0] & sets[2]) and not (sets[1] & sets[2]), "stages overlap"
    if split.train_idx.size:
        assert k.iloc[split.train_idx].max() < split.cal_start, "a training label is known inside calibration"
    if split.cal_idx.size:
        assert o.iloc[split.cal_idx].min() >= split.cal_start
        assert k.iloc[split.cal_idx].max() < split.test_start, "a calibration label is known inside test"
    if split.test_idx.size:
        assert o.iloc[split.test_idx].min() >= split.test_start

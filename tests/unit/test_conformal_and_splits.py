import dataclasses

import numpy as np
import pandas as pd
import pytest
from tessera_core.forecast import SplitConformal, conformal_quantile, make_split, rolling_test_windows
from tessera_core.forecast.splits import assert_split_clean
from tessera_evalkit.interval_metrics import coverage, interval_pinball, pinball, wape


def test_conformal_quantile_hand_computed() -> None:
    scores = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0])
    # n=9, coverage .8 -> k = ceil(10 * .8) = 8 -> 8th smallest = 8
    assert conformal_quantile(scores, 0.8) == 8.0
    # n=4, coverage .8 -> k = ceil(5 * .8) = 4 -> largest
    assert conformal_quantile(np.array([4.0, 1.0, 3.0, 2.0]), 0.8) == 4.0


def test_conformal_rejects_too_few_or_bad_input() -> None:
    with pytest.raises(ValueError):
        conformal_quantile(np.array([1.0, 2.0, 3.0]), 0.8)  # k=4 > n=3
    with pytest.raises(ValueError):
        conformal_quantile(np.array([1.0, np.nan, 3.0, 4.0, 5.0]), 0.8)
    with pytest.raises(ValueError):
        conformal_quantile(np.array([1.0]), 1.0)


def test_conformal_empirical_coverage_iid() -> None:
    rng = np.random.default_rng(1)
    cal_y, test_y = rng.normal(size=5_000), rng.normal(size=50_000)
    cp = SplitConformal(0.8).calibrate(cal_y, np.zeros_like(cal_y))
    lo, up = cp.interval(np.zeros_like(test_y))
    assert abs(coverage(test_y, lo, up) - 0.8) < 0.01


def test_mondrian_groups_and_fallback_and_state_roundtrip() -> None:
    rng = np.random.default_rng(2)
    n = 400
    g = np.array(["A"] * 200 + ["B"] * 190 + ["C"] * 10)
    scale = np.where(g == "A", 1.0, np.where(g == "B", 5.0, 1.0))
    y = rng.normal(size=n) * scale
    cp = SplitConformal(0.8, min_group_size=30).calibrate(y, np.zeros(n), g.tolist())
    assert cp.q_by_group["B"] > 2 * cp.q_by_group["A"]
    assert "C" not in cp.q_by_group  # too small -> global fallback
    w = cp.half_width(3, ["A", "C", "Z"])
    assert w[1] == w[2] == cp.q_global
    clone = SplitConformal.from_state(cp.state())
    assert clone.state() == cp.state()


def test_uncalibrated_raises() -> None:
    with pytest.raises(RuntimeError):
        SplitConformal().interval(np.zeros(3))


def _origins() -> pd.Series:
    return pd.Series(pd.date_range("2022-01-03", periods=150, freq="W-MON"))


@pytest.mark.parametrize("h", [4, 8, 13])
def test_split_disjoint_chronological_with_embargo(h: int) -> None:
    t = _origins()
    ts, te = t.iloc[110], t.iloc[130]
    s = make_split(t, h, ts, te, cal_weeks=20)
    assert s.train_idx.size and s.cal_idx.size and s.test_idx.size
    assert not set(s.train_idx) & set(s.cal_idx)
    assert not set(s.cal_idx) & set(s.test_idx)
    # labels realised before the next stage starts
    assert (t.iloc[s.train_idx] + pd.Timedelta(weeks=h)).max() < s.cal_start
    assert (t.iloc[s.cal_idx] + pd.Timedelta(weeks=h)).max() < s.test_start
    # the embargo removes exactly the rows whose labels would leak
    assert t.iloc[s.train_idx].max() < s.cal_start - pd.Timedelta(weeks=h) + pd.Timedelta(days=1)


def test_leaky_split_is_detected() -> None:
    t = _origins()
    s = make_split(t, 8, t.iloc[110], t.iloc[130], cal_weeks=20)
    leaky = dataclasses.replace(s, cal_idx=np.concatenate([s.cal_idx, s.test_idx[:2]]))
    with pytest.raises(AssertionError):
        assert_split_clean(t, leaky)
    leaky2 = dataclasses.replace(s, train_idx=np.concatenate([s.train_idx, s.cal_idx[:1]]))
    with pytest.raises(AssertionError):
        assert_split_clean(t, leaky2)


def test_rolling_windows_contiguous() -> None:
    w = rolling_test_windows(_origins(), n_folds=3, test_frac=0.3)
    assert len(w) == 3
    for (_, e), (s, _) in zip(w, w[1:], strict=False):
        assert e == s
    assert all(a < b for a, b in w)


def test_metrics_hand_computed() -> None:
    y = np.array([10.0, -10.0, 20.0])
    p = np.array([12.0, -5.0, 20.0])
    assert wape(y, p) == pytest.approx(7.0 / 40.0)
    assert pinball(np.array([1.0]), np.array([0.0]), 0.9) == pytest.approx(0.9)
    assert pinball(np.array([0.0]), np.array([1.0]), 0.9) == pytest.approx(0.1)
    lo, up = np.array([0.0]), np.array([2.0])
    assert interval_pinball(np.array([3.0]), lo, up, 0.8) == pytest.approx(0.5 * (0.1 * 3 + 0.9 * 1))

"""Evaluation metrics for the risk components."""

from __future__ import annotations

import numpy as np
import pytest
from sklearn.metrics import brier_score_loss, roc_auc_score
from tessera_evalkit.risk_metrics import (
    auc,
    brier,
    classifier_report,
    expected_calibration_error,
    quantile_coverage,
    reliability_table,
)

RNG = np.random.default_rng(0)
P = RNG.uniform(0, 1, 5000)
Y = (RNG.uniform(0, 1, 5000) < P).astype(float)  # perfectly calibrated by construction


def test_auc_and_brier_match_sklearn_including_ties() -> None:
    assert auc(Y, P) == pytest.approx(roc_auc_score(Y, P))
    ties = np.round(P, 1)
    assert auc(Y, ties) == pytest.approx(roc_auc_score(Y, ties))
    assert brier(Y, P) == pytest.approx(brier_score_loss(Y, P))


def test_auc_edge_cases() -> None:
    assert auc([0, 1, 0, 1], [0.1, 0.9, 0.2, 0.8]) == 1.0
    assert auc([0, 1], [0.9, 0.1]) == 0.0
    assert auc([0, 1, 0, 1], [0.5] * 4) == 0.5
    assert np.isnan(auc([1, 1, 1], [0.2, 0.5, 0.9]))


def test_ece_is_small_when_calibrated_and_large_when_overconfident() -> None:
    assert expected_calibration_error(Y, P) < 0.03
    assert expected_calibration_error(Y, np.clip(P * 1.0, 0, 1)) < expected_calibration_error(
        Y, np.where(P > 0.5, 0.99, 0.01)
    )
    assert expected_calibration_error([0, 1, 0, 1], [1.0, 0.0, 1.0, 0.0]) == pytest.approx(1.0)


def test_reliability_table_counts_and_edges() -> None:
    t = reliability_table(Y, P, 10)
    assert int(t["n"].sum()) == len(Y)
    assert (t["bin_hi"] - t["bin_lo"]).round(9).eq(0.1).all()
    edge = reliability_table([1, 0], [1.0, 0.0], 10)  # p == 1.0 must land in the last bin
    assert edge["bin_hi"].max() == pytest.approx(1.0) and int(edge["n"].sum()) == 2


def test_input_validation() -> None:
    with pytest.raises(ValueError, match="only 0 and 1"):
        brier([0, 2], [0.1, 0.2])
    with pytest.raises(ValueError, match="probabilities"):
        brier([0, 1], [0.1, 1.2])
    with pytest.raises(ValueError, match="equal length"):
        auc([0, 1], [0.1])
    with pytest.raises(ValueError, match="empty"):
        brier([], [])
    with pytest.raises(ValueError, match="n_bins"):
        reliability_table(Y, P, 1)


def test_classifier_report_keys() -> None:
    r = classifier_report(Y, P)
    assert set(r) == {"n", "base_rate", "auc", "brier", "ece"} and r["n"] == len(Y)


def test_quantile_coverage_is_nominal_for_a_correct_forecaster() -> None:
    rng = np.random.default_rng(1)
    realized = rng.normal(size=20000)
    levels = (5, 25, 50, 75, 95)
    pred = np.tile(np.array([-1.645, -0.6745, 0.0, 0.6745, 1.645]), (len(realized), 1))
    cov = quantile_coverage(realized, pred, levels)
    assert (cov["gap"].abs() < 0.01).all() and list(cov["level"]) == list(levels)
    biased = quantile_coverage(realized, pred - 1.0, levels)  # forecasts 1 sigma too low -> too few hits
    # theory: P(Z <= q - 1) - nominal = -0.046, -0.204, -0.341, -0.371, -0.205 (the 5% tail has little mass to lose)
    assert (biased["gap"] < -0.04).all() and biased["gap"].iloc[2] < -0.3
    with pytest.raises(ValueError, match="shape"):
        quantile_coverage(realized, pred[:, :3], levels)

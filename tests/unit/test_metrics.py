import math

import pytest

from tessera_evalkit.metrics import bias, mae, smape, wape

Y = [100.0, 200.0, 300.0]
P = [110.0, 180.0, 330.0]  # errors +10, -20, +30


def test_wape_hand_computed() -> None:
    assert wape(Y, P) == pytest.approx(60 / 600)


def test_mae_hand_computed() -> None:
    assert mae(Y, P) == pytest.approx(20.0)


def test_bias_sign_positive_is_over_forecast() -> None:
    assert bias(Y, P) == pytest.approx(20 / 3)


def test_smape_hand_computed() -> None:
    expected = (2 * 10 / 210 + 2 * 20 / 380 + 2 * 30 / 630) / 3
    assert smape(Y, P) == pytest.approx(expected)


def test_wape_nan_when_all_actuals_zero() -> None:
    assert math.isnan(wape([0.0, 0.0], [1.0, 2.0]))


def test_smape_zero_over_zero_is_perfect() -> None:
    assert smape([0.0, 10.0], [0.0, 10.0]) == 0.0


def test_wape_not_dominated_by_near_zero_week() -> None:
    # One near-zero actual would make MAPE explode (error 500 / 1 = 50000%).
    y, p = [1.0, 1000.0, 1000.0], [501.0, 1000.0, 1000.0]
    assert wape(y, p) == pytest.approx(500 / 2001)


def test_shape_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        wape([1.0], [1.0, 2.0])

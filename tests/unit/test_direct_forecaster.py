from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from synth_panel import FEATURES, HORIZONS, concrete_features, make_panel, naive, seasonal_naive_factory
from tessera_core.forecast import DirectForecaster, ForecastResult, load, make_split, save, target_col
from tessera_core.forecast.results import to_decimal
from tessera_evalkit.forecast_eval import evaluate_forecaster

FAST_GRID = (
    {"num_leaves": 7, "learning_rate": 0.1, "n_estimators": 60, "min_child_samples": 20},
    {"num_leaves": 15, "learning_rate": 0.1, "n_estimators": 60, "min_child_samples": 20},
)


@pytest.fixture(scope="module")
def panel() -> tuple[pd.DataFrame, dict[tuple[str, str], np.ndarray]]:
    return make_panel(n_businesses=8, n_weeks=200, seed=3)


def _fit(frame: pd.DataFrame, test_start: pd.Timestamp) -> DirectForecaster:
    return DirectForecaster(HORIZONS, FEATURES, 0.8, seed=0, grid=FAST_GRID).fit(frame, test_start, cal_weeks=26)


def test_forecast_result_validation_and_decimals() -> None:
    r = ForecastResult(
        business_id="b",
        currency="USD",
        horizon_weeks=4,
        point=to_decimal(0.1 + 0.2),
        lower=Decimal("0.00"),
        upper=Decimal("1.00"),
        target_coverage=0.8,
        model_version="v",
    )
    assert r.point == Decimal("0.30")
    with pytest.raises(ValueError):
        ForecastResult(
            business_id="b",
            currency="USD",
            horizon_weeks=4,
            point=Decimal("5"),
            lower=Decimal("6"),
            upper=Decimal("7"),
            target_coverage=0.8,
            model_version="v",
        )
    with pytest.raises(ValueError):
        to_decimal(float("nan"))


def test_fit_predict_invariants(panel: tuple[pd.DataFrame, dict[tuple[str, str], np.ndarray]]) -> None:
    frame, _ = panel
    t0 = pd.Timestamp(frame["as_of"].quantile(0.8, interpolation="nearest"))
    fc = _fit(frame, t0)
    test = frame[frame["as_of"] >= t0].reset_index(drop=True)
    widths = []
    for h in HORIZONS:
        p = fc.predict_horizon(test, h)
        assert (p["lower"] <= p["point"]).all() and (p["point"] <= p["upper"]).all()
        assert np.isfinite(p.to_numpy()).all()
        widths.append(float((p["upper"] - p["lower"]).mean()))
    # uncertainty should not shrink with horizon (allow 5% slack: with a seasonal lag, h8 vs h13
    # can be near-equal on short-memory synthetic noise) and the longest must beat the shortest
    assert widths[2] > widths[0] and widths[1] > widths[0], widths
    assert widths[2] > 0.95 * widths[1], widths
    results = fc.forecast(test.head(5))
    assert len(results) == 5 * len(HORIZONS)
    assert {r.model_version for r in results} == {fc.version}


def test_calibration_never_overlaps_train_or_test(
    panel: tuple[pd.DataFrame, dict[tuple[str, str], np.ndarray]],
) -> None:
    frame, _ = panel
    t0 = pd.Timestamp(frame["as_of"].quantile(0.8, interpolation="nearest"))
    for h in HORIZONS:
        s = make_split(frame["as_of"], h, t0, t0 + pd.Timedelta(weeks=8), cal_weeks=26)
        tr, ca, te = map(set, (s.train_idx.tolist(), s.cal_idx.tolist(), s.test_idx.tolist()))
        assert not (tr & ca) and not (tr & te) and not (ca & te)
        t = frame["as_of"]
        assert (t.iloc[s.train_idx] + pd.Timedelta(weeks=h)).max() < t.iloc[s.cal_idx].min()
        assert (t.iloc[s.cal_idx] + pd.Timedelta(weeks=h)).max() < t.iloc[s.test_idx].min()


def test_fit_is_blind_to_test_period(panel: tuple[pd.DataFrame, dict[tuple[str, str], np.ndarray]]) -> None:
    """Scramble every row from test_start on (features AND labels): the fitted model must not change."""
    frame, _ = panel
    t0 = pd.Timestamp(frame["as_of"].quantile(0.8, interpolation="nearest"))
    a = _fit(frame, t0)
    mutated = frame.copy()
    late = mutated["as_of"] >= t0
    rng = np.random.default_rng(0)
    for col in [*concrete_features(), *[target_col(h) for h in HORIZONS]]:
        mutated.loc[late, col] = rng.normal(1e6, 1e5, late.sum())
    b = _fit(mutated, t0)
    assert a.version == b.version


def test_deterministic_and_artifact_roundtrip(
    tmp_path: Path, panel: tuple[pd.DataFrame, dict[tuple[str, str], np.ndarray]]
) -> None:
    frame, _ = panel
    t0 = pd.Timestamp(frame["as_of"].quantile(0.8, interpolation="nearest"))
    a, b = _fit(frame, t0), _fit(frame, t0)
    assert a.version == b.version and a.version is not None
    save(a, tmp_path / "m")
    c = load(tmp_path / "m")
    test = frame[frame["as_of"] >= t0].head(50).reset_index(drop=True)
    for h in HORIZONS:
        pd.testing.assert_frame_equal(a.predict_horizon(test, h), c.predict_horizon(test, h))
    assert c.version == a.version
    # tamper with a model file -> hash mismatch
    f = tmp_path / "m" / "model_h4.txt"
    f.write_text(f.read_text().replace("leaf_value=", "leaf_value= ", 1))
    with pytest.raises(ValueError, match="hash mismatch"):
        load(tmp_path / "m")


def test_version_changes_with_calibration(panel: tuple[pd.DataFrame, dict[tuple[str, str], np.ndarray]]) -> None:
    frame, _ = panel
    t0 = pd.Timestamp(frame["as_of"].quantile(0.8, interpolation="nearest"))
    a = _fit(frame, t0)
    b = DirectForecaster(HORIZONS, FEATURES, 0.9, seed=0, grid=FAST_GRID).fit(frame, t0, cal_weeks=26)
    assert a.version != b.version


def test_missing_columns_and_unfitted() -> None:
    with pytest.raises(KeyError):
        DirectForecaster((4,), ["x"]).fit(
            pd.DataFrame({"business_id": [], "currency": [], "as_of": []}), pd.Timestamp("2023-01-02"), 10
        )
    with pytest.raises(KeyError):
        DirectForecaster((4,), ["x"]).predict_horizon(pd.DataFrame(), 4)


def test_backtest_coverage_and_baselines(panel: tuple[pd.DataFrame, dict[tuple[str, str], np.ndarray]]) -> None:
    frame, series = panel
    summary, rows = evaluate_forecaster(
        frame,
        HORIZONS,
        FEATURES,
        {"naive": naive, "snaive": seasonal_naive_factory(series)},
        n_folds=3,
        test_frac=0.3,
        cal_weeks=26,
        seed=0,
    )
    overall = summary[summary["currency"] == "ALL"].set_index("horizon")
    print(overall.round(3).to_string())
    assert set(overall.index) == set(HORIZONS)
    assert (overall["coverage"].between(0.72, 0.88)).all(), overall["coverage"]
    assert (overall["wape_lgbm"] < overall["wape_naive"]).all()
    assert (overall["wape_lgbm"] < overall["wape_snaive"]).all()
    w = overall["mean_width"].to_numpy()
    assert w[-1] > w[0] and (w[1:] > 0.95 * w[:-1]).all(), w
    # backtest rows keep calibration out of test: each fold used one fitted version per horizon
    assert rows.groupby(["fold", "horizon"])["model_version"].nunique().eq(1).all()

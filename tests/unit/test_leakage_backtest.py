import pandas as pd
import pytest
from helpers import make_history

from tessera_core.forecast.backtest import iter_folds, rolling_origin_backtest
from tessera_core.forecast.baselines import BASELINES
from tessera_core.forecast.features import FEATURE_COLUMNS, build_features
from tessera_core.forecast.history import History

AS_OF = pd.Timestamp("2025-09-28")  # a Sunday


def _poison_future(h: History, t: pd.Timestamp) -> History:
    """Copy of h with every record after t modified."""
    cash = h.cashflows.copy()
    late = cash["cash_date"] > t
    cash.loc[late, "amount"] = cash.loc[late, "amount"] * -7.3 + 12345.0
    extra = cash[late].copy()
    extra["cash_date"] = extra["cash_date"] + pd.Timedelta(days=1)
    cash = pd.concat([cash, extra], ignore_index=True)

    inv = h.invoices.copy()
    after_issue = inv["issue_date"] > t
    inv.loc[after_issue, "amount"] = 9.99e6
    inv.loc[after_issue, "currency"] = "EUR"
    # invoices open at t but paid after t: change when (or whether) they were paid
    paid_after = inv["paid_date"] > t
    inv.loc[paid_after, "paid_date"] = inv.loc[paid_after, "paid_date"] + pd.Timedelta(days=40)
    inv.loc[inv["paid_date"].isna() & ~after_issue, "paid_date"] = t + pd.Timedelta(days=3)
    return History(cashflows=cash, invoices=inv)


def test_features_identical_when_future_is_modified() -> None:
    h = make_history()
    original = build_features(h, AS_OF)
    poisoned = build_features(_poison_future(h, AS_OF), AS_OF)
    assert not original.empty
    pd.testing.assert_frame_equal(original, poisoned)


def test_features_equal_on_pre_truncated_history() -> None:
    h = make_history()
    pd.testing.assert_frame_equal(build_features(h, AS_OF), build_features(h.as_of(AS_OF), AS_OF))


def test_feature_schema_and_nan_policy() -> None:
    f = build_features(make_history(), AS_OF)
    assert list(f.columns) == FEATURE_COLUMNS
    assert f["lag_1"].notna().all()  # 38+ weeks of history => lags 1-12 present


def test_open_invoices_depend_only_on_what_was_paid_by_as_of() -> None:
    h = make_history()
    f = build_features(h, AS_OF)
    inv = h.invoices
    open_at_t = inv[(inv["issue_date"] <= AS_OF) & ~(inv["paid_date"] <= AS_OF) & (inv["business_id"] == "biz0")]
    assert f.loc[("biz0", "USD"), "open_total"] == pytest.approx(open_at_t["amount"].sum())


def test_folds_strictly_chronological() -> None:
    h = make_history()
    folds = list(iter_folds(h))
    assert folds
    for fd in folds:
        assert fd.train_end < fd.test_start
    origins = [fd.origin for fd in folds]
    assert origins == sorted(origins)
    assert {fd.horizon for fd in folds} == {4, 8, 13}


def test_backtest_predictions_ignore_the_future() -> None:
    h = make_history()
    base = rolling_origin_backtest(h, BASELINES)
    origin = pd.Timestamp(base["origin"].min())
    # Poison everything after the first origin; predictions made at that origin must not move.
    poisoned = rolling_origin_backtest(_poison_future(h, origin), BASELINES)
    key = ["business_id", "currency", "origin", "horizon", "model"]
    a = base[base["origin"] == origin].sort_values(key).reset_index(drop=True)
    b = poisoned[poisoned["origin"] == origin].sort_values(key).reset_index(drop=True)
    pd.testing.assert_series_equal(a["y_pred"], b["y_pred"])


def test_backtest_y_true_matches_hand_sum() -> None:
    h = make_history()
    res = rolling_origin_backtest(h, {"naive": BASELINES["naive"]}, horizons=(4,))
    row = res.iloc[0]
    start = pd.Timestamp(row["origin"]) + pd.Timedelta(days=1)
    end = pd.Timestamp(row["origin"]) + pd.Timedelta(weeks=4)
    c = h.cashflows
    expected = c[
        (c["business_id"] == row["business_id"]) & (c["currency"] == row["currency"])
        & (c["cash_date"] >= start) & (c["cash_date"] < end + pd.Timedelta(days=1))
    ]["amount"].sum()
    assert row["y_true"] == pytest.approx(expected)

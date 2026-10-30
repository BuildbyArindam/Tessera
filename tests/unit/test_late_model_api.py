"""Scorer fit/calibration and the read-only ``assess_late_payment`` tool."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from synth_risk import make_invoice_table
from tessera_core.risk import (
    InsufficientDataError,
    InvoiceSelectionError,
    InvoiceTableError,
    LatePaymentAssessment,
    assess_late_payment,
    build_snapshot_dataset,
    chrono_split,
    fit_late_scorer,
    prepare,
)
from tessera_core.risk.late_model import P_MAX, P_MIN, fit_calibrated

D = pd.Timestamp


@pytest.fixture(scope="module")
def world():  # noqa: ANN201
    inv, end = make_invoice_table(seed=0, n_customers=30, months=20)
    ds = build_snapshot_dataset(prepare(inv), end)
    cs, ts, te = D("2025-09-01"), D("2025-12-01"), D("2026-09-01")
    sp = chrono_split(ds["origin"], ds["label_known"], cs, ts, te)
    train, cal, test = ds.iloc[sp.train_idx], ds.iloc[sp.cal_idx], ds.iloc[sp.test_idx]
    return inv, end, train, cal, test, fit_late_scorer(train, cal, seed=0)


def _auc(y: np.ndarray, p: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(y, p))


def test_both_models_separate_prompt_from_chronic_customers(world) -> None:  # noqa: ANN001
    _, _, _, _, test, scorer = world
    for name in ("logreg", "lgbm"):
        assert _auc(test["y"].to_numpy(), scorer.predict(test, name)) > 0.75, name


def test_probabilities_are_clipped_and_calibration_is_fitted_on_cal_only(world) -> None:  # noqa: ANN001
    _, _, train, cal, test, scorer = world
    for name, m in scorer.models.items():
        p = m.predict_proba(test)
        assert p.min() >= P_MIN and p.max() <= P_MAX, name
        raw_cal = m.predict_raw(cal)
        assert m.calibrator.X_min_ == pytest.approx(raw_cal.min())  # isotonic knots come from cal rows
        assert m.calibrator.X_max_ == pytest.approx(raw_cal.max())
        assert not np.isclose(m.calibrator.X_min_, m.predict_raw(train).min()) or len(train) == len(cal)


def test_isotonic_cannot_be_worse_in_sample_and_small_windows_are_flagged(world) -> None:  # noqa: ANN001
    """What is guaranteed: on the window it was fitted on, isotonic never loses to the identity map.

    What is NOT guaranteed, and was measured on this very world (81 calibration invoices): held-out
    Brier can get WORSE (0.141 raw -> 0.157 calibrated for LightGBM). So a small window must be flagged.
    """
    _, _, _, cal, _, scorer = world
    y = cal["y"].to_numpy()
    for name, m in scorer.models.items():
        raw = float(np.mean((m.predict_raw(cal) - y) ** 2))
        fitted = float(np.mean((m.predict_proba(cal) - y) ** 2))
        assert fitted <= raw + 1e-3, f"{name}: in-sample calibrated Brier {fitted:.4f} vs raw {raw:.4f}"
    assert cal["invoice_id"].nunique() < 200
    assert any("only" in n and "invoices" in n for n in scorer.notes)


def test_fit_is_deterministic(world) -> None:  # noqa: ANN001
    _, _, train, cal, test, scorer = world
    again = fit_late_scorer(train, cal, seed=0)
    np.testing.assert_array_equal(scorer.predict(test), again.predict(test))


def test_insufficient_or_single_class_data_is_rejected(world) -> None:  # noqa: ANN001
    _, _, train, cal, _, _ = world
    with pytest.raises(InsufficientDataError, match="training stage has 50 rows"):
        fit_calibrated("lgbm", train.head(50), cal)
    with pytest.raises(InsufficientDataError, match="single class"):
        fit_calibrated("lgbm", train, cal.assign(y=1.0))
    few = cal[cal["invoice_id"].isin(cal["invoice_id"].unique()[:10])]
    few = pd.concat([few] * 4, ignore_index=True)  # plenty of rows, but only 10 distinct invoices
    assert len(few) >= 100 and few["y"].nunique() == 2
    with pytest.raises(InsufficientDataError, match="covers 10 invoices"):
        fit_calibrated("lgbm", train, few)
    with pytest.raises(KeyError):
        world[5].predict(train.head(3), "nope")  # type: ignore[arg-type]


def test_assess_returns_valid_models_with_sane_ranges(world) -> None:  # noqa: ANN001
    inv, _, _, _, _, scorer = world
    t = D("2026-01-15")
    out = assess_late_payment(scorer, inv, t)
    assert out and all(isinstance(a, LatePaymentAssessment) for a in out)
    open_ids = set(
        prepare(inv[inv["issue_date"] <= t].assign(paid_date=inv["paid_date"].where(inv["paid_date"] <= t)))[
            "invoice_id"
        ]
    )
    assert {a.invoice_id for a in out} <= open_ids
    for a in out:
        assert 0.0 <= a.p_late <= 1.0
        assert a.already_late == (t.date() > a.due_date + pd.Timedelta(days=7))
        if a.already_late:
            assert a.p_late == 1.0 and a.model == "deterministic"
        else:
            assert P_MIN <= a.p_late <= P_MAX
        assert a.pay_date_low is not None and a.pay_date_median is not None and a.pay_date_high is not None
        assert t.date() < a.pay_date_low <= a.pay_date_median <= a.pay_date_high


def test_assess_ignores_everything_after_as_of(world) -> None:  # noqa: ANN001
    inv, _, _, _, _, scorer = world
    t = D("2026-01-15")
    full = assess_late_payment(scorer, inv, t)
    truncated = assess_late_payment(
        scorer, inv[inv["issue_date"] <= t].assign(paid_date=inv["paid_date"].where(inv["paid_date"] <= t)), t
    )
    assert [a.model_dump() for a in full] == [a.model_dump() for a in truncated]


def test_assess_selection_errors_and_model_override(world) -> None:  # noqa: ANN001
    inv, _, _, _, _, scorer = world
    t = D("2026-01-15")
    one = assess_late_payment(scorer, inv, t)[0]
    with pytest.raises(InvoiceSelectionError, match="NOPE"):
        assess_late_payment(scorer, inv, t, ["NOPE"])
    paid_before = inv[inv["paid_date"] < t].iloc[0]["invoice_id"]
    with pytest.raises(InvoiceSelectionError):
        assess_late_payment(scorer, inv, t, [paid_before])  # already paid on that date
    lr = assess_late_payment(scorer, inv, t, [one.invoice_id], model="logreg")
    assert len(lr) == 1 and lr[0].invoice_id == one.invoice_id
    assert lr[0].model in ("logreg", "deterministic")
    assert assess_late_payment(scorer, inv, D("2020-01-01")) == []  # nothing issued yet


def test_assess_result_is_immutable_and_json_ready(world) -> None:  # noqa: ANN001
    inv, _, _, _, _, scorer = world
    a = assess_late_payment(scorer, inv, D("2026-01-15"))[0]
    with pytest.raises(Exception, match="frozen"):
        a.p_late = 0.0  # type: ignore[misc]
    assert LatePaymentAssessment.model_validate_json(a.model_dump_json()) == a


def test_null_required_fields_are_a_typed_error_not_nan(world) -> None:  # noqa: ANN001
    inv, _, _, _, _, scorer = world
    for col in ("due_date", "currency", "customer_id"):
        bad = inv.copy()
        bad.loc[bad.index[0], col] = pd.NaT if col == "due_date" else None
        with pytest.raises(InvoiceTableError, match="null values"):
            assess_late_payment(scorer, bad, D("2026-01-15"))
    bad = inv.copy()
    bad.loc[bad.index[0], "amount"] = float("nan")
    with pytest.raises(InvoiceTableError, match="strictly positive"):
        assess_late_payment(scorer, bad, D("2026-01-15"))


def test_unseen_customer_and_unseen_business_get_finite_scores_and_fallback_ranges(world) -> None:  # noqa: ANN001
    inv, _, _, _, _, scorer = world
    t = D("2026-01-15")
    new = pd.DataFrame(
        {
            "invoice_id": ["N1", "N2"], "business_id": ["B0", "NOBODY"], "customer_id": ["NEW", "NEW"],
            "currency": ["USD", "EUR"], "amount": [500.0, 700.0], "issue_date": [D("2026-01-02")] * 2,
            "due_date": [D("2026-02-01")] * 2, "paid_date": [pd.NaT] * 2,
        }
    )  # fmt: skip
    got = {a.invoice_id: a for a in assess_late_payment(scorer, pd.concat([inv, new]), t, ["N1", "N2"])}
    assert got["N1"].range_source == "business" and got["N2"].range_source == "global"
    assert all(0 < a.p_late < 1 for a in got.values())

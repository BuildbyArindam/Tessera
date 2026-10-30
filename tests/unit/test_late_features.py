"""Labels, leakage control and the chronological split for the late-payment scorer."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from synth_risk import make_invoice_table
from tessera_core.risk import InvoiceTableError, build_snapshot_dataset, chrono_split, prepare
from tessera_core.risk.late_features import GBM_FEATURES, add_labels, features_at, mask_as_of, validate_invoices

D = pd.Timestamp


def _one(due: str, paid: str | None) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "invoice_id": ["I1"],
            "business_id": ["B"],
            "customer_id": ["C"],
            "currency": ["USD"],
            "amount": [100.0],
            "issue_date": [D("2025-01-01")],
            "due_date": [D(due)],
            "paid_date": [D(paid) if paid else pd.NaT],
        }
    )


@pytest.mark.parametrize(
    ("paid", "late", "known"),
    [
        ("2025-02-01", 0.0, "2025-02-01"),  # paid on the due date
        ("2025-02-08", 0.0, "2025-02-08"),  # exactly 7 days late is NOT late
        ("2025-02-09", 1.0, "2025-02-09"),  # 8 days late is late (known on due + 8 either way)
        ("2025-03-30", 1.0, "2025-02-09"),  # very late: known late as soon as it is still unpaid on due + 8
        (None, 1.0, "2025-02-09"),  # never paid
    ],
)
def test_label_boundary_and_known_time(paid: str | None, late: float, known: str) -> None:
    out = add_labels(_one("2025-02-01", paid))
    assert out["is_late"].iloc[0] == late
    assert out["label_known"].iloc[0] == D(known)


def test_validation_rejects_bad_tables() -> None:
    good = _one("2025-02-01", None)
    with pytest.raises(InvoiceTableError, match="missing columns"):
        validate_invoices(good.drop(columns=["amount"]))
    with pytest.raises(InvoiceTableError, match="strictly positive"):
        validate_invoices(good.assign(amount=[0.0]))
    with pytest.raises(InvoiceTableError, match="precedes"):
        validate_invoices(good.assign(due_date=[D("2024-12-01")]))
    with pytest.raises(InvoiceTableError, match="datetime64"):
        validate_invoices(good.assign(due_date=["2025-02-01"]))
    with pytest.raises(InvoiceTableError, match="unique"):
        validate_invoices(pd.concat([good, good]))


def test_features_identical_on_full_and_masked_table() -> None:
    """The core leakage test: features at T must not change when the future is deleted."""
    inv, end = make_invoice_table(seed=1)
    full = prepare(inv)
    for t in (D("2025-06-15"), D("2025-09-01"), end - pd.Timedelta(days=60)):
        rows = full[(full["issue_date"] <= t) & (full["paid_date"].isna() | (full["paid_date"] > t))]
        rows = rows[t <= rows["due_date"] + pd.Timedelta(days=7)]
        masked = prepare(mask_as_of(validate_invoices(inv), t))
        rows_masked = masked[masked["invoice_id"].isin(rows["invoice_id"])]
        assert len(rows_masked) == len(rows) > 0
        a = features_at(full, t, rows).set_index(rows["invoice_id"]).sort_index()
        b = features_at(masked, t, rows_masked).set_index(rows_masked["invoice_id"]).sort_index()
        pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-9, atol=1e-12)


def test_future_payments_cannot_change_features() -> None:
    inv, _ = make_invoice_table(seed=2)
    t = D("2025-08-01")
    full = prepare(inv)
    rows = full[(full["issue_date"] <= t) & (full["paid_date"].isna() | (full["paid_date"] > t))]
    rows = rows[t <= rows["due_date"] + pd.Timedelta(days=7)]
    before = features_at(full, t, rows)
    tampered = inv.copy()
    future = tampered["paid_date"] > t
    tampered.loc[future, "paid_date"] = tampered.loc[future, "paid_date"] + pd.Timedelta(days=300)  # rewrite the future
    after = features_at(prepare(tampered), t, rows)
    pd.testing.assert_frame_equal(before, after)


def test_snapshot_rows_are_open_unlabelled_and_uncensored() -> None:
    inv, end = make_invoice_table(seed=3)
    prepared = prepare(inv)
    ds = build_snapshot_dataset(prepared, end)
    m = ds.merge(prepared[["invoice_id", "due_date", "paid_date", "issue_date"]], on="invoice_id")
    assert (m["origin"] >= m["issue_date"]).all()
    assert (m["paid_date"].isna() | (m["paid_date"] > m["origin"])).all(), "snapshot of an already-paid invoice"
    assert (m["origin"] <= m["due_date"] + pd.Timedelta(days=7)).all(), "row whose label is already known"
    assert (ds["label_known"] > ds["origin"]).all(), "label must be unknown at the snapshot"
    assert (ds["label_known"] <= end).all(), "censored invoice leaked into the dataset"
    assert set(GBM_FEATURES) <= set(ds.columns)
    assert ds["y"].isin([0.0, 1.0]).all()


def test_first_invoice_amount_ratio_is_neutral() -> None:
    inv, _ = make_invoice_table(seed=4)
    p = prepare(inv)
    first = p.sort_values(["issue_date", "invoice_id"]).groupby(["business_id", "customer_id"]).head(1)
    assert (first["log_amount_ratio"] == 0.0).all()
    assert np.isfinite(p["log_amount_ratio"]).all()


def test_chrono_split_embargo_and_bad_boundaries() -> None:
    inv, end = make_invoice_table(seed=5)
    ds = build_snapshot_dataset(prepare(inv), end)
    cs, ts, te = D("2025-10-01"), D("2025-12-15"), D("2026-03-01")
    sp = chrono_split(ds["origin"], ds["label_known"], cs, ts, te)
    assert len(sp.train_idx) and len(sp.cal_idx) and len(sp.test_idx)
    assert ds["label_known"].iloc[sp.train_idx].max() < cs
    assert ds["label_known"].iloc[sp.cal_idx].max() < ts
    assert ds["origin"].iloc[sp.test_idx].min() >= ts
    # embargo bites: a naive split on origin alone would have put these rows in training
    naive_train = ds.index[ds["origin"] < cs]
    assert len(naive_train) > len(sp.train_idx)
    with pytest.raises(ValueError, match="cal_start < test_start"):
        chrono_split(ds["origin"], ds["label_known"], ts, cs, te)

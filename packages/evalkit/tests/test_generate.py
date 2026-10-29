from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pyarrow.parquet as pq
import pytest

from tessera_evalkit import fx as fxmod
from tessera_evalkit import generate as gen
from tessera_evalkit.records import (
    BusinessRec, CustomerRec, FxRateRec, GroundTruthRec, InvoiceRec, OutflowRec, PaymentRec,
)

MONTHS = 18


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    root = tmp_path_factory.mktemp("d7")
    m = gen.generate(7, root, fx_source="synthetic")
    rd = lambda n: pq.read_table(root / "synthetic" / f"{n}.parquet").to_pylist()
    return root, m, {n: rd(n) for n in m["rows"]}


def test_determinism_same_seed_identical_bytes(tmp_path, ds):
    _, m1, _ = ds
    m2 = gen.generate(7, tmp_path, fx_source="synthetic")
    assert m1["sha256"] == m2["sha256"]
    a = (ds[0] / "fx" / "fx_daily.parquet").read_bytes()
    assert a == (tmp_path / "fx" / "fx_daily.parquet").read_bytes()


def test_different_seed_differs(tmp_path, ds):
    m3 = gen.generate(8, tmp_path, fx_source="synthetic")
    assert m3["sha256"]["invoices"] != ds[1]["sha256"]["invoices"]


def test_shape(ds):
    _, m, t = ds
    assert m["rows"]["businesses"] == 12
    archs = [b["archetype"] for b in t["businesses"]]
    assert {a: archs.count(a) for a in set(archs)} == {
        "solo_freelancer": 4, "small_agency": 4, "product_exporter": 4}
    assert m["as_of"] == "2026-06-30"


@pytest.mark.parametrize("name,model", [
    ("businesses", BusinessRec), ("customers", CustomerRec), ("invoices", InvoiceRec),
    ("payments", PaymentRec), ("ground_truth", GroundTruthRec), ("outflows", OutflowRec)])
def test_parquet_rows_revalidate(ds, name, model):
    for row in ds[2][name][:500]:
        model.model_validate(row)


def test_amounts_exact_decimals_and_positive(ds):
    for tbl in ("invoices", "payments", "outflows"):
        for r in ds[2][tbl]:
            assert isinstance(r["amount"], Decimal) and r["amount"] > 0
            assert r["amount"] == r["amount"].quantize(Decimal("0.01"))


def test_referential_integrity(ds):
    t = ds[2]
    inv = {i["invoice_id"]: i for i in t["invoices"]}
    cust = {c["customer_id"]: c for c in t["customers"]}
    assert len(inv) == len(t["invoices"])
    for i in inv.values():
        assert cust[i["customer_id"]]["business_id"] == i["business_id"]
        assert cust[i["customer_id"]]["currency"] == i["currency"]
    for p in t["payments"]:
        assert inv[p["invoice_id"]]["currency"] == p["currency"]
        assert p["pay_date"] > inv[p["invoice_id"]]["invoice_date"]
    assert {g["invoice_id"] for g in t["ground_truth"]} == set(inv)


def test_currencies(ds):
    t = ds[2]
    assert {i["currency"] for i in t["invoices"]} == {"USD", "EUR", "GBP"}
    pay = [o for o in t["outflows"] if o["kind"] == "payroll"]
    assert {o["currency"] for o in pay} == {"INR"}
    assert {o["currency"] for o in t["outflows"] if o["kind"] == "supplier_invoice"} == {"SGD", "AUD"}
    assert {o["currency"] for o in t["outflows"] if o["kind"] == "saas_card"} <= {"USD", "EUR"}


def test_payroll_fixed_day_every_month(ds):
    t = ds[2]
    for b in t["businesses"]:
        rows = [o for o in t["outflows"] if o["business_id"] == b["business_id"] and o["kind"] == "payroll"]
        assert len(rows) == MONTHS
        for o in rows:  # fixed day, only ever rolled BACK onto a business day
            assert o["pay_date"].weekday() < 5
            assert 0 <= (_target(o["pay_date"], b["payroll_day"]) - o["pay_date"]).days <= 3


def _target(d: date, day: int) -> date:
    # nearest month's nominal payroll date at or after d (d was rolled back <=2 days)
    for off in range(0, 4):
        c = d + timedelta(days=off)
        if c.day == min(day, _last(c)):
            return c
    return d


def _last(d: date) -> int:
    import calendar
    return calendar.monthrange(d.year, d.month)[1]


def test_bad_outcome_rate_about_two_percent(ds):
    gt = ds[2]["ground_truth"]
    bad = sum(g["outcome"] != "paid" for g in gt) / len(gt)
    assert 0.012 <= bad <= 0.03


def test_customer_types_present_per_business_and_ordered_delays(ds):
    import statistics as st
    t = ds[2]
    for b in t["businesses"]:
        types = {g["customer_type"] for g in t["ground_truth"] if g["business_id"] == b["business_id"]}
        assert {"chronic_late", "seasonal_late", "prompt"} <= types
    med = lambda ty: st.median(g["days_past_due"] for g in t["ground_truth"]
                               if g["customer_type"] == ty and g["outcome"] == "paid")
    assert med("prompt") < med("typical") < med("chronic_late")
    assert med("chronic_late") > 20


def test_delays_right_skewed_lognormal_like(ds):
    import statistics as st
    d = [g["days_past_due"] for g in ds[2]["ground_truth"]
         if g["customer_type"] == "typical" and g["outcome"] == "paid"]
    assert st.mean(d) > st.median(d)


def test_seasonality_december_dip_and_quarter_end_spike(ds):
    from collections import Counter
    c = Counter((i["invoice_date"].year, i["invoice_date"].month) for i in ds[2]["invoices"])
    # compare within 2025 only to avoid growth confounding the spike test
    nov, dec = c[(2025, 11)], c[(2025, 12)]
    assert dec < 0.85 * nov
    qe = sum(c[(2025, m)] for m in (3, 6, 9)) / 3
    non = sum(c[(2025, m)] for m in (2, 4, 5, 7, 8)) / 5
    assert qe > 1.05 * non


def test_growth_trend(ds):
    inv = ds[2]["invoices"]
    first = sum(i["invoice_date"] < date(2025, 7, 1) for i in inv)
    last = sum(i["invoice_date"] >= date(2026, 1, 1) for i in inv)
    assert last > first


def test_open_invoices_have_no_payment_yet_and_future_latent(ds):
    t = ds[2]
    paid_ids = {p["invoice_id"] for p in t["payments"]}
    as_of = date(2026, 6, 30)
    gt = {g["invoice_id"]: g for g in t["ground_truth"]}
    for i in t["invoices"]:
        if i["status"] == "open" and gt[i["invoice_id"]]["outcome"] == "paid":
            assert i["invoice_id"] not in paid_ids
            assert gt[i["invoice_id"]]["latent_pay_date"] > as_of
    assert all(p["pay_date"] <= as_of for p in t["payments"])


# ------------------------------------------------------------------ FX ---
def test_fx_synthetic_labelled_and_forward_filled(ds):
    root = ds[0]
    meta = json.loads((root / "fx" / "fx_meta.json").read_text())
    assert meta["source"] == "synthetic" and "SYNTHETIC" in meta["note"]
    rows = pq.read_table(root / "fx" / "fx_daily.parquet").to_pylist()
    assert {r["source"] for r in rows} == {"synthetic"}
    for r in rows[:2000]:
        FxRateRec.model_validate(r)
    eurusd = [r for r in rows if r["pair"] == "EURUSD"]
    days = [r["date"] for r in eurusd]
    assert days == sorted(days) and all((b - a).days == 1 for a, b in zip(days, days[1:]))
    by = {r["date"]: r for r in eurusd}
    for d, r in by.items():
        if d.weekday() >= 5:
            assert not r["is_observed"] and r["rate"] == by[d - timedelta(days=d.weekday() - 4)]["rate"]
        else:
            assert r["is_observed"]


def test_fx_table_cross_rates(ds):
    t = fxmod.table_from_parquet(ds[0] / "fx" / "fx_daily.parquet")
    d = date(2025, 6, 2)
    assert t.rate(d, "USD", "USD") == 1.0
    assert t.rate(d, "USD", "EUR") * t.rate(d, "EUR", "USD") == pytest.approx(1.0)
    assert t.rate(d, "USD", "INR") == pytest.approx(t.rate(d, "USD", "EUR") * t.rate(d, "EUR", "INR"))


def test_fx_env_var_switch_and_ecb_strict(tmp_path, monkeypatch):
    monkeypatch.setenv(fxmod.ENV_VAR, "synthetic")
    t = fxmod.load_fx(date(2025, 1, 1), date(2025, 3, 31), seed=1, data_dir=tmp_path)
    assert t.source == "synthetic"
    monkeypatch.setenv(fxmod.ENV_VAR, "bogus")
    with pytest.raises(ValueError):
        fxmod.load_fx(date(2025, 1, 1), date(2025, 3, 31), seed=1, data_dir=tmp_path)
    monkeypatch.setenv(fxmod.ENV_VAR, "ecb")
    monkeypatch.setattr(fxmod, "_fetch_frankfurter",
                        lambda *a, **k: (_ for _ in ()).throw(fxmod.FxUnavailable("offline")))
    with pytest.raises(fxmod.FxUnavailable):
        fxmod.load_fx(date(2025, 1, 1), date(2025, 3, 31), seed=1, data_dir=tmp_path / "x")


def test_ecb_path_via_cached_raw_file(tmp_path, monkeypatch):
    """Real-source path, offline: a raw Frankfurter file on disk is parsed and forward-filled."""
    raw = tmp_path / "fx" / "raw"
    raw.mkdir(parents=True)
    payload = {"base": "EUR", "start_date": "2025-01-02", "end_date": "2025-01-08", "rates": {
        "2025-01-02": {"USD": 1.04, "GBP": 0.83, "INR": 89.0, "SGD": 1.41, "AUD": 1.67},
        "2025-01-03": {"USD": 1.03, "GBP": 0.83, "INR": 88.9, "SGD": 1.41, "AUD": 1.66},
        "2025-01-06": {"USD": 1.03, "GBP": 0.83, "INR": 88.7, "SGD": 1.41, "AUD": 1.66}}}
    (raw / "frankfurter_EUR_2024-12-25_2025-01-08.json").write_text(json.dumps(payload))
    monkeypatch.setattr(fxmod, "_fetch_frankfurter", lambda *a, **k: pytest.fail("network used"))
    t = fxmod.load_fx(date(2025, 1, 1), date(2025, 1, 8), seed=1, data_dir=tmp_path, source="ecb")
    assert t.source == "ecb"
    assert t.rate(date(2025, 1, 4), "EUR", "USD") == 1.03   # Saturday forward-filled from Friday
    assert t.rate(date(2025, 1, 5), "EUR", "USD") == 1.03
    assert t.rate(date(2025, 1, 2), "EUR", "USD") == 1.04


def test_parse_frankfurter_rejects_garbage():
    with pytest.raises(ValueError):
        fxmod.parse_frankfurter({"base": "USD", "rates": {}})

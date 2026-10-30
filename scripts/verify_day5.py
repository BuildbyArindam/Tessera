"""Day 5 verification. Run: python scripts/verify_day5.py   (exit code 1 if any criterion FAILS)"""

from __future__ import annotations

import math
import sys

import numpy as np
import pandas as pd

sys.path[:0] = ["packages/core", "packages/evalkit", "tests", "tests/unit"]

from tessera_core.risk import (  # noqa: E402
    GBM_FEATURES,
    InvoiceSelectionError,
    InvoiceTableError,
    assess_late_payment,
    build_snapshot_dataset,
    estimate_fx_range,
    horizon_steps,
    prepare,
)
from tessera_core.risk.late_features import features_at, mask_as_of, validate_invoices  # noqa: E402
from tessera_evalkit import fx_backtest  # noqa: E402
from tessera_evalkit.risk_data import load_invoice_table  # noqa: E402
from tessera_evalkit.risk_eval import FOLDS, _fold_predictions  # noqa: E402
from tessera_evalkit.risk_metrics import auc, brier, expected_calibration_error, reliability_table  # noqa: E402
from test_fx_range import garch_rates  # noqa: E402

D = pd.Timestamp
failures: list[str] = []
pd.set_option("display.width", 200)


def check(ok: bool, msg: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {msg}")
    if not ok:
        failures.append(msg)


def finite(a) -> bool:  # noqa: ANN001
    return all(math.isfinite(x) for x in np.asarray(a, dtype=float).ravel())


inv, end = load_invoice_table("data/synthetic")
ds = build_snapshot_dataset(prepare(inv), end)
parts, scorers = [], {}
for i, (s, e) in enumerate(FOLDS, 1):
    p, scorers[i] = _fold_predictions(ds, i, s, e, seed=0)
    parts.append(p)
preds = pd.concat(parts, ignore_index=True)
y = preds["y"].to_numpy()

print("== 1. AUC and Brier: calibrated LightGBM vs base-rate predictor (pooled held-out folds) ==")
rows = []
for name in ("base_rate", "cust_history", "p_logreg", "p_lgbm"):
    rows.append({"predictor": name, "n": len(y), "auc": auc(y, preds[name]), "brier": brier(y, preds[name])})
print(pd.DataFrame(rows).round(4).to_string(index=False))
b_base, b_lgbm, b_lr = (brier(y, preds[c]) for c in ("base_rate", "p_lgbm", "p_logreg"))
check(b_lgbm < b_base, f"LightGBM Brier {b_lgbm:.4f} < base-rate Brier {b_base:.4f}")
check(b_lr < b_base, f"logistic Brier {b_lr:.4f} < base-rate Brier {b_base:.4f}")
for k in sorted(preds["fold"].unique()):
    g = preds[preds["fold"] == k]
    check(brier(g["y"], g["p_lgbm"]) < brier(g["y"], g["base_rate"]), f"fold {k}: LightGBM Brier below base rate")

print("\n== 2. Reliability table (10 bins, calibrated LightGBM) and ECE ==")
t = reliability_table(y, preds["p_lgbm"], 10)
t["gap"] = t["frac_pos"] - t["mean_pred"]
print(t.round(3).to_string(index=False))
ece = expected_calibration_error(y, preds["p_lgbm"], 10)
print(
    f"  ECE (calibrated LightGBM) = {ece:.4f}; uncalibrated = {expected_calibration_error(y, preds['p_lgbm_raw']):.4f}"
)
check(ece < 0.05, f"ECE {ece:.4f} < 0.05")

print("\n== 3. FX quantile coverage in the backtest (target: each within 0.04 of nominal) ==")
rows_fx = fx_backtest.run_backtest("data/fx/fx_daily.parquet")
cov = fx_backtest.coverage_summary(rows_fx)
q = cov[cov["level"].apply(lambda v: isinstance(v, int))].copy()
q["level"] = q["level"].astype(int)
print("  SYNTHETIC FX series (Frankfurter blocked in this sandbox), 6 pairs, observed share <= quantile:")
print(q.pivot(index="level", columns="horizon_days", values="observed").round(3).to_string())
print("  gap vs nominal:")
print(q.pivot(index="level", columns="horizon_days", values="gap").round(3).to_string())
for h, g in q.groupby("horizon_days"):
    worst = g["gap"].abs().max()
    check(worst <= 0.04, f"synthetic FX backtest {h}d: worst |gap| {worst:.3f} <= 0.04")

print("\n  Same estimator on a process with KNOWN volatility dynamics (GARCH, t(6) shocks, 5 paths pooled):")
for h in (7, 30):
    steps, hits, n = horizon_steps(h), np.zeros(5), 0
    for seed in range(100, 105):
        r = garch_rates(5000, seed)
        for o in range(250, len(r) - steps, steps):
            fr = estimate_fx_range(r[: o + 1], pair="S", horizon_days=h, n_sims=1500, seed=o)
            hits += [100 * (r[o + steps] / r[o] - 1) <= x.change_pct for x in fr.quantiles]
            n += 1
    gaps = hits / n - np.array([0.05, 0.25, 0.5, 0.75, 0.95])
    print(f"  {h:>2}d n={n}: observed {(hits / n).round(3).tolist()}  gaps {gaps.round(3).tolist()}")
    check(np.abs(gaps).max() <= 0.04, f"known-process {h}d: worst |gap| {np.abs(gaps).max():.3f} <= 0.04")


def seed_experiment(zero_drift: bool, horizons: tuple[int, ...] = (7, 30, 90)) -> dict[int, np.ndarray]:
    """20 generator seeds x 6 pairs; pooled coverage per level (between-seed spread = honest uncertainty)."""
    from datetime import date

    import tessera_evalkit.fx as fxmod

    saved = dict(fxmod._DRIFT)
    if zero_drift:
        fxmod._DRIFT = {k: 0.0 for k in saved}
    try:
        out: dict[int, list[np.ndarray]] = {h: [] for h in horizons}
        for seed in range(1, 21):
            obs = fxmod.synthetic_observations(seed, date(2023, 12, 25), date(2026, 6, 30))
            days = sorted(obs)
            for h in horizons:
                steps, hits, n = horizon_steps(h), np.zeros(5), 0
                for pair in fx_backtest.PAIRS:
                    b, qc = pair[:3], pair[3:]
                    r = np.array(
                        [(1.0 if qc == "EUR" else obs[d][qc]) / (1.0 if b == "EUR" else obs[d][b]) for d in days]
                    )
                    for i in range(120, len(r) - steps, 5):
                        fr = estimate_fx_range(r[: i + 1], pair=pair, horizon_days=h, n_sims=1500, seed=i)
                        hits += [100 * (r[i + steps] / r[i] - 1) <= x.change_pct for x in fr.quantiles]
                        n += 1
                out[h].append(hits / n)
        return {h: np.array(v).mean(0) for h, v in out.items()}
    finally:
        fxmod._DRIFT = saved


nominal = np.array([0.05, 0.25, 0.5, 0.75, 0.95])
print(
    "\n  Why it fails: the synthetic generator hard-codes INR depreciation (3%/yr);"
    " the estimator is zero-drift by design."
)
print("  Controlled experiment, 20 generator seeds x 6 pairs, observed coverage at 5/25/50/75/95:")
for zd in (False, True):
    res = seed_experiment(zd)
    for h, m in res.items():
        print(
            f"  {'drift REMOVED' if zd else 'as generated '} {h:>2}d: {m.round(3).tolist()}"
            f"  worst|gap| {np.abs(m - nominal).max():.3f}"
        )
        if zd:
            check(
                np.abs(m - nominal).max() <= 0.04,
                f"generator drift removed, {h}d: worst |gap| {np.abs(m - nominal).max():.3f} <= 0.04",
            )

print("\n== 4. No feature uses information after the as-of date ==")
check(
    not {"y", "label_known", "is_late", "paid_date", "origin", "invoice_id"} & set(GBM_FEATURES),
    "no label/outcome column is a model feature",
)
full = prepare(inv)
issue_dates = pd.Series(sorted(full["issue_date"].unique())).sample(40, random_state=0)
bad = 0
for t0 in issue_dates:  # snapshot AT the invoice date: features may only use facts known that day
    t0 = D(t0)
    rows_t = full[full["issue_date"] == t0]
    masked = prepare(mask_as_of(validate_invoices(inv), t0))
    rows_m = masked[masked["invoice_id"].isin(rows_t["invoice_id"])]
    a = features_at(full, t0, rows_t).set_index(rows_t["invoice_id"]).sort_index()
    b = features_at(masked, t0, rows_m).set_index(rows_m["invoice_id"]).sort_index()
    bad += int(not np.allclose(a.to_numpy(float), b.to_numpy(float), equal_nan=True))
check(
    bad == 0,
    f"features at each invoice's own issue date identical with all later data deleted (40 dates, {bad} differ)",
)
t1 = D("2026-02-10")
rows_t = full[(full["issue_date"] <= t1) & (full["paid_date"].isna() | (full["paid_date"] > t1))]
rows_t = rows_t[t1 <= rows_t["due_date"] + pd.Timedelta(days=7)]
tamper = inv.copy()
fut = tamper["paid_date"] > t1
tamper.loc[fut, "paid_date"] += pd.Timedelta(days=400)
tamper.loc[tamper["issue_date"] > t1, "amount"] *= 1000
same = features_at(full, t1, rows_t).equals(features_at(prepare(tamper), t1, rows_t))
check(same, "rewriting every payment and invoice after the as-of date leaves features unchanged")
print("  NOTE: features also use OTHER invoices' outcomes known by the as-of date (customer history); the rule is")
print("        'nothing after the as-of date', not 'nothing after this invoice's own issue date'.")

print("\n== 5. Edge cases: clear result or typed error, never NaN ==")
sc, T = scorers[2], D("2026-04-15")
base_out = assess_late_payment(sc, inv, T)


def all_finite(out) -> bool:  # noqa: ANN001
    return all(math.isfinite(a.p_late) and 0 <= a.p_late <= 1 for a in out)


check(
    len(base_out) > 0 and all_finite(base_out), f"baseline: {len(base_out)} open invoices, every p_late finite in [0,1]"
)

new = pd.DataFrame(
    [
        {
            "invoice_id": "NEW-1",
            "business_id": "B01",
            "customer_id": "BRAND-NEW",
            "currency": "USD",
            "amount": 1234.5,
            "issue_date": D("2026-04-01"),
            "due_date": D("2026-05-01"),
            "paid_date": pd.NaT,
        },
        {
            "invoice_id": "NEW-2",
            "business_id": "UNSEEN-BIZ",
            "customer_id": "C-X",
            "currency": "EUR",
            "amount": 99.0,
            "issue_date": D("2026-04-01"),
            "due_date": D("2026-05-01"),
            "paid_date": pd.NaT,
        },
    ]
)
out = {
    a.invoice_id: a for a in assess_late_payment(sc, pd.concat([inv, new], ignore_index=True), T, ["NEW-1", "NEW-2"])
}
for k, a in out.items():
    print(
        f"  {k}: p_late={a.p_late:.3f} model={a.model} range={a.pay_date_low}..{a.pay_date_high} "
        f"source={a.range_source}"
    )
    check(math.isfinite(a.p_late) and a.pay_date_low is not None, f"{k} (no history): finite p_late and a date range")
check(out["NEW-1"].range_source == "business", "customer with no history falls back to its business curve")
check(out["NEW-2"].range_source == "global", "business with no history falls back to the global curve")
solo = new.iloc[:1]
a = assess_late_payment(sc, solo, T)[0]
print(f"  single invoice, no history anywhere: p_late={a.p_late:.3f} range={a.pay_date_low} note={a.note!r}")
check(
    math.isfinite(a.p_late) and a.pay_date_low is None and a.note is not None,
    "no history at all: finite p_late, no range, explanatory note",
)

paid_id = inv[inv["paid_date"] < T].iloc[0]["invoice_id"]
try:
    assess_late_payment(sc, inv, T, [paid_id])
    check(False, "already-paid invoice requested -> typed error")
except InvoiceSelectionError as e:
    check(True, f"already-paid invoice requested -> InvoiceSelectionError ({str(e)[:60]}...)")
check(
    paid_id not in {a.invoice_id for a in base_out},
    "already-paid invoices are never scored when listing all open invoices",
)
future_paid = inv[inv["paid_date"] > T].iloc[0]["invoice_id"]
check(
    future_paid in {a.invoice_id for a in base_out},
    "an invoice paid only AFTER the as-of date is still treated as open",
)

for label, mutate, err in (
    ("null amount", lambda d: d.assign(amount=d["amount"].where(d.index != 0)), InvoiceTableError),
    ("null due_date", lambda d: d.assign(due_date=d["due_date"].where(d.index != 0)), InvoiceTableError),
    ("null currency", lambda d: d.assign(currency=d["currency"].where(d.index != 0)), InvoiceTableError),
    ("negative amount", lambda d: d.assign(amount=-d["amount"]), InvoiceTableError),
):
    try:
        r = assess_late_payment(sc, mutate(inv.copy()), T)
        check(False, f"{label}: expected typed error, got {len(r)} results (nan-safe? {all_finite(r)})")
    except err:
        check(True, f"{label} -> {err.__name__}")
    except Exception as e:  # noqa: BLE001
        check(False, f"{label}: untyped {type(e).__name__}: {e}")

print("\n  FX data gap:")
idx = pd.bdate_range("2024-01-01", periods=400)
s = pd.Series(np.exp(np.cumsum(np.random.default_rng(0).normal(0, 0.004, 400))) * 83.0, index=idx, name="USDINR")
gap = s.copy()
gap.iloc[200:230] = np.nan
try:
    estimate_fx_range(gap.to_numpy(), pair="USDINR", horizon_days=30)
    check(False, "NaN gap passed straight to estimator: expected typed error")
except ValueError as e:
    check(True, f"NaN gap in rates -> {type(e).__name__}: {e}")
try:
    fr = fx_backtest.series_from_frame(s.drop(s.index[200:230]).rename("USDINR"))
    check(False, f"30-business-day hole silently accepted ({len(fr)} rates)")
except fx_backtest.FxDataGapError as e:
    check(True, f"30-business-day hole in a loaded series -> FxDataGapError: {e}")
tiny = fx_backtest.series_from_frame(s.drop(s.index[100:102]).rename("USDINR"))
check(len(tiny) == 398, "a 2-day hole (a holiday) is tolerated")
fr = estimate_fx_range(tiny.to_numpy(), pair="USDINR", horizon_days=30)
check(
    finite([fr.spot, fr.daily_vol_pct] + [x.value for x in fr.quantiles] + [p.probability for p in fr.p_fall]),
    "estimator output on the holiday-gap series is entirely finite",
)

print(
    "\n"
    + ("ALL CRITERIA PASSED" if not failures else f"{len(failures)} CRITERIA FAILED:\n  - " + "\n  - ".join(failures))
)
sys.exit(1 if failures else 0)

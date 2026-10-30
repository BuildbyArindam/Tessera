"""Day 5 evaluation: late-payment scorer and FX range estimator.

    python -m tessera_evalkit.risk_eval --data data --out docs/risk_eval

Late-payment: three expanding chronological folds. For each fold the calibration window is the 91
days before the test window, and training uses only snapshots whose label was known before the
calibration window began (see ``late_split``). Reported per fold and pooled, on two views:
``all`` snapshots, and ``at_issue`` (the first snapshot of each invoice; one independent row per
invoice, so it is not dominated by long-dated invoices that have many snapshots).

Baselines make the numbers interpretable: ``base_rate`` (training late rate for everyone) and
``cust_history`` (the customer's shrunk historical late rate, no model).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from tessera_core.risk import assess_late_payment, build_snapshot_dataset, chrono_split, fit_late_scorer, prepare
from tessera_core.risk.late_model import LateScorer

from .fx_backtest import coverage_by_pair, coverage_summary, run_backtest
from .risk_data import load_invoice_table
from .risk_metrics import classifier_report
from .risk_plots import plot_reliability

FOLDS = (("2026-01-01", "2026-03-01"), ("2026-03-01", "2026-05-01"), ("2026-05-01", "2026-07-01"))
CAL_DAYS = 91
PREDICTORS = ("base_rate", "cust_history", "p_logreg_raw", "p_logreg", "p_lgbm_raw", "p_lgbm")
RANGE_ASOF = ("2026-01-01", "2026-02-01", "2026-03-01")  # >= 120 days before data end: few unresolved payments


def _fold_predictions(ds: pd.DataFrame, fold: int, start: str, end: str, seed: int) -> tuple[pd.DataFrame, LateScorer]:
    ts, te = pd.Timestamp(start), pd.Timestamp(end)
    sp = chrono_split(ds["origin"], ds["label_known"], ts - pd.Timedelta(days=CAL_DAYS), ts, te)
    train, cal, test = ds.iloc[sp.train_idx], ds.iloc[sp.cal_idx], ds.iloc[sp.test_idx]
    scorer = fit_late_scorer(train, cal, seed=seed)
    out = test[["invoice_id", "origin", "y"]].copy()
    out["fold"] = fold
    out["base_rate"] = float(train["y"].mean())
    out["cust_history"] = test["cust_late_rate"].to_numpy()
    for name in ("logreg", "lgbm"):
        m = scorer.models[name]
        out[f"p_{name}_raw"] = m.predict_raw(test)
        out[f"p_{name}"] = m.predict_proba(test)
    return out, scorer


def _metrics(preds: pd.DataFrame) -> pd.DataFrame:
    first = preds.sort_values("origin").drop_duplicates("invoice_id")
    rows = []
    for view, frame in (("all", preds), ("at_issue", first)):
        for fold, g in [*frame.groupby("fold"), ("pooled", frame)]:
            for p in PREDICTORS:
                rows.append({"view": view, "fold": fold, "predictor": p, **classifier_report(g["y"], g[p])})
    return pd.DataFrame(rows)


def _range_eval(inv: pd.DataFrame, scorers: dict[int, LateScorer]) -> pd.DataFrame:
    """Coverage of the 10-90% payment-date range among open invoices that were eventually paid."""
    paid = inv.set_index("invoice_id")["paid_date"]
    rows = []
    for asof in RANGE_ASOF:
        t = pd.Timestamp(asof)
        fold = 1 if t < pd.Timestamp(FOLDS[1][0]) else 2
        for a in assess_late_payment(scorers[fold], inv, t):
            real = paid.get(a.invoice_id)
            if a.pay_date_low is None or a.pay_date_high is None or a.pay_date_median is None or pd.isna(real):
                continue
            d = real.date()
            rows.append(
                {
                    "as_of": asof,
                    "source": a.range_source,
                    "inside": a.pay_date_low <= d <= a.pay_date_high,
                    "abs_err_median_days": abs((d - a.pay_date_median).days),
                    "width_days": (a.pay_date_high - a.pay_date_low).days,
                }
            )
    df = pd.DataFrame(rows)
    g = (
        df.groupby("source")
        .agg(
            n=("inside", "size"),
            coverage=("inside", "mean"),
            mae_days=("abs_err_median_days", "mean"),
            width_days=("width_days", "mean"),
        )
        .reset_index()
    )
    total = pd.DataFrame(
        [
            {
                "source": "ALL",
                "n": len(df),
                "coverage": df["inside"].mean(),
                "mae_days": df["abs_err_median_days"].mean(),
                "width_days": df["width_days"].mean(),
            }
        ]
    )
    return pd.concat([g, total], ignore_index=True)


def run(data_dir: Path, out_dir: Path, seed: int = 0) -> dict[str, pd.DataFrame]:
    inv, data_end = load_invoice_table(data_dir / "synthetic")
    ds = build_snapshot_dataset(prepare(inv), data_end)
    parts, scorers = [], {}
    for i, (s, e) in enumerate(FOLDS, 1):
        p, scorers[i] = _fold_predictions(ds, i, s, e, seed)
        parts.append(p)
    preds = pd.concat(parts, ignore_index=True)
    metrics = _metrics(preds)
    rel = plot_reliability(
        preds,
        out_dir / "reliability.png",
        subtitle=f"3 chronological folds pooled, n={len(preds)} snapshots; synthetic data (seed in manifest)",
    )
    fx_rows = run_backtest(data_dir / "fx" / "fx_daily.parquet")
    fx_cov = coverage_summary(fx_rows)
    results = {
        "late_metrics": metrics,
        "late_reliability": rel,
        "late_range_eval": _range_eval(inv, scorers),
        "fx_coverage": fx_cov,
        "fx_coverage_by_pair": coverage_by_pair(fx_rows),
        "fx_backtest_rows": fx_rows,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, df in results.items():
        df.to_csv(out_dir / f"{name}.csv", index=False)
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tessera_evalkit.risk_eval")
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--out", type=Path, default=Path("docs/risk_eval"))
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    res = run(a.data, a.out, a.seed)
    pd.set_option("display.width", 200)
    m = res["late_metrics"]
    for view in ("all", "at_issue"):
        t = m[(m["view"] == view) & (m["fold"] == "pooled")].set_index("predictor")
        print(f"\n== late-payment, pooled test, view={view} ==")
        print(t[["n", "base_rate", "auc", "brier", "ece"]].round(4).to_string())
    print("\n== late-payment, per fold (all snapshots, calibrated LightGBM) ==")
    print(
        m[(m["view"] == "all") & (m["predictor"] == "p_lgbm")][["fold", "n", "auc", "brier", "ece"]]
        .round(4)
        .to_string(index=False)
    )
    print("\n== payment-date range (10-90%, nominal 80%) ==")
    print(res["late_range_eval"].round(3).to_string(index=False))
    print("\n== FX quantile coverage (pooled over pairs; SYNTHETIC rates unless fx_meta says ecb) ==")
    c = res["fx_coverage"]
    print(c.pivot(index="level", columns="horizon_days", values="observed").round(3).to_string())
    print("n_eff per horizon:", c.groupby("horizon_days")["n_eff"].first().round(1).to_dict())
    bp = res["fx_coverage_by_pair"]
    print("\n== FX by pair at 90d: share of outcomes below q05 / q50 / q95 (nominal .05 / .50 / .95) ==")
    print(bp[bp["horizon_days"] == 90].drop(columns="horizon_days").round(3).to_string(index=False))
    print("NOTE: one sample path per pair; the estimator assumes zero drift, so trending pairs miss q50 by design.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

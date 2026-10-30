"""Day 4 verification: backtest report + hard assertions. Run: python scripts/verify_day4.py"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path[:0] = ["packages/core", "packages/evalkit", "tests"]

from synth_panel import FEATURES, HORIZONS, make_panel, naive, seasonal_naive_factory  # noqa: E402

from tessera_core.forecast import make_split, rolling_test_windows  # noqa: E402
from tessera_evalkit.forecast_eval import evaluate_forecaster  # noqa: E402
from tessera_evalkit.interval_metrics import wape  # noqa: E402

CAL_WEEKS, N_FOLDS, TEST_FRAC, TARGET = 26, 3, 0.3, 0.8
pd.set_option("display.width", 200)

frame, series = make_panel(n_businesses=8, n_weeks=200, seed=3)
summary, rows = evaluate_forecaster(
    frame,
    HORIZONS,
    FEATURES,
    {"naive": naive, "snaive": seasonal_naive_factory(series)},
    n_folds=N_FOLDS,
    test_frac=TEST_FRAC,
    cal_weeks=CAL_WEEKS,
    coverage_target=TARGET,
    seed=0,
)
failures: list[str] = []

print("== 1. WAPE: LightGBM vs seasonal-naive (and naive), overall ==")
overall = summary[summary.currency == "ALL"].set_index("horizon")
print(overall[["n", "wape_lgbm", "wape_snaive", "wape_naive"]].round(3).to_string())

print("\n== 2. Empirical coverage of the 80% interval, per horizon x currency ==")
cov = summary.pivot(index="currency", columns="horizon", values="coverage").round(3)
print(cov.to_string())
for h, c in overall["coverage"].items():
    if not 0.77 <= c <= 0.83:
        failures.append(f"overall coverage h={h}: {c:.3f} outside [0.77, 0.83]")
rows["hit"] = (rows.y >= rows.lower) & (rows.y <= rows.upper)
print("\ncoverage by fold (drift check):")
print(rows.pivot_table(index="fold", columns="horizon", values="hit").round(3).to_string())

print("\n== 3. Interval ordering lower <= point <= upper (all test rows) ==")
bad = ~((rows.lower <= rows.lgbm) & (rows.lgbm <= rows.upper))
print(f"rows checked: {len(rows)}, violations: {int(bad.sum())}")
if bad.any():
    failures.append(f"{int(bad.sum())} rows violate lower <= point <= upper")

print("\n== 4. Mean interval width by horizon ==")
widths = (rows.upper - rows.lower).groupby(rows.horizon).mean()
print(widths.round(1).to_string())
if not widths.is_monotonic_increasing:
    failures.append(f"mean width does not strictly grow with horizon: {widths.round(1).tolist()}")

print("\n== 5. Calibration never overlaps train or test (every fold x horizon) ==")
dates = pd.to_datetime(frame.as_of)
n_checked = 0
for t0, t1 in rolling_test_windows(frame.as_of, N_FOLDS, TEST_FRAC):
    for h in HORIZONS:
        s = make_split(frame.as_of, h, t0, t1, CAL_WEEKS)
        tr, ca, te = (set(a.tolist()) for a in (s.train_idx, s.cal_idx, s.test_idx))
        assert not (tr & ca) and not (ca & te) and not (tr & te), "row overlap"
        d = lambda idx: dates.iloc[idx]  # noqa: E731
        assert (d(s.train_idx) + pd.Timedelta(weeks=h)).max() < d(s.cal_idx).min(), "train label in cal window"
        assert (d(s.cal_idx) + pd.Timedelta(weeks=h)).max() < d(s.test_idx).min(), "cal label in test window"
        n_checked += 1
        print(
            f"  h={h:>2} {t0.date()}: train<= {d(s.train_idx).max().date()} | cal "
            f"{d(s.cal_idx).min().date()}..{d(s.cal_idx).max().date()} (n={len(ca)}) | "
            f"test {d(s.test_idx).min().date()}.. (n={len(te)})  overlap=0"
        )
print(f"{n_checked} splits verified: disjoint rows and no label realised across a stage boundary")

print("\n== 6. Businesses where LightGBM loses to seasonal-naive (WAPE) ==")
rec = []
for (b, h), g in rows.groupby(["business_id", "horizon"]):
    y = g.y.to_numpy()
    rec.append({"business": b, "h": h, "lgbm": wape(y, g.lgbm.to_numpy()), "snaive": wape(y, g.snaive.to_numpy())})
per_b = pd.DataFrame(rec)
per_b["loses"] = per_b.lgbm > per_b.snaive
print(per_b.pivot(index="business", columns="h", values="loses").to_string())
print(per_b[per_b.loses].round(3).to_string(index=False) if per_b.loses.any() else "none")
print(f"losses: {int(per_b.loses.sum())} of {len(per_b)} business-horizon pairs")
rec2 = []
for (b, c, h), g in rows.groupby(["business_id", "currency", "horizon"]):
    rec2.append({"b": b, "c": c, "h": h, "lgbm": wape(g.y.to_numpy(), g.lgbm.to_numpy()),
                 "snaive": wape(g.y.to_numpy(), g.snaive.to_numpy())})
s2 = pd.DataFrame(rec2)
print(f"at business x currency level: {int((s2.lgbm > s2.snaive).sum())} of {len(s2)} lose")

print("\n== RESULT ==")
if failures:
    print("FAIL:\n  " + "\n  ".join(failures))
    sys.exit(1)
print("PASS")

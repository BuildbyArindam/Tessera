<<<<<<< HEAD
"""Summarise backtest results and write the baseline results table.

    python -m tessera_evalkit.forecast_eval --data data/synthetic --out docs/baseline_results.csv
=======
"""Rolling-origin evaluation of the LightGBM + conformal forecaster against baselines.

Each fold: test window = a contiguous block of origins at the end of history; the model is
refit on data whose labels are realised before the calibration window (expanding window).
Calibration rows never overlap train or test rows (see tessera_core.forecast.splits).
>>>>>>> b5b2d1a (Day 4: LightGBM direct forecaster with split conformal intervals)
"""

from __future__ import annotations

<<<<<<< HEAD
import argparse
from pathlib import Path

import pandas as pd

from tessera_core.forecast.backtest import DEFAULT_HORIZONS, rolling_origin_backtest
from tessera_core.forecast.baselines import BASELINES
from tessera_core.forecast.history import History
from tessera_evalkit.loading import load_history
from tessera_evalkit.metrics import bias, mae, smape, wape


def summarize(results: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Metrics per group. WAPE is primary; ``n`` counts forecast cases."""

    def agg(g: pd.DataFrame) -> pd.Series:
        y, p = g["y_true"], g["y_pred"]
        return pd.Series(
            {"wape": wape(y, p), "smape": smape(y, p), "mae": mae(y, p), "bias": bias(y, p), "n": len(g)}
        )

    return results.groupby(by, sort=True).apply(agg, include_groups=False).reset_index()


def run_baseline_eval(
    history: History, horizons: tuple[int, ...] = DEFAULT_HORIZONS, min_train_weeks: int = 26, step_weeks: int = 4
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (raw results, summary by horizon x model)."""
    raw = rolling_origin_backtest(history, BASELINES, horizons, min_train_weeks, step_weeks)
    return raw, summarize(raw, ["horizon", "model"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="data/synthetic")
    ap.add_argument("--out", default="docs/baseline_results.csv")
    ap.add_argument("--min-train-weeks", type=int, default=26)
    ap.add_argument("--step-weeks", type=int, default=4)
    args = ap.parse_args()

    raw, summary = run_baseline_eval(load_history(args.data), min_train_weeks=args.min_train_weeks, step_weeks=args.step_weeks)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(out, index=False)
    raw.to_csv(out.with_name(out.stem + "_raw.csv"), index=False)
    print(summary.to_string(index=False))
    print(f"\nwrote {out} and {out.stem}_raw.csv")


if __name__ == "__main__":
    main()
=======
from collections.abc import Callable, Mapping, Sequence

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from tessera_core.forecast import DirectForecaster, make_split, rolling_test_windows, target_col

from tessera_evalkit.interval_metrics import (
    coverage,
    interval_pinball,
    mean_width,
    wape,
)

FloatArray = NDArray[np.float64]
Baseline = Callable[[pd.DataFrame, int], FloatArray]


def evaluate_forecaster(
    frame: pd.DataFrame,
    horizons: Sequence[int],
    feature_cols: Sequence[str],
    baselines: Mapping[str, Baseline],
    n_folds: int = 3,
    test_frac: float = 0.3,
    cal_weeks: int = 26,
    coverage_target: float = 0.8,
    seed: int = 0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (summary, rows).

    summary: one row per (horizon, currency|ALL) with WAPE for each model, empirical coverage,
    mean width and pinball loss. rows: every test prediction (for plots / audits).
    """
    frame = frame.reset_index(drop=True)
    windows = rolling_test_windows(frame["as_of"], n_folds, test_frac)
    parts: list[pd.DataFrame] = []
    for fold, (t0, t1) in enumerate(windows):
        fc = DirectForecaster(horizons, feature_cols, coverage_target, seed).fit(frame, t0, cal_weeks)
        for h in horizons:
            split = make_split(frame["as_of"], h, t0, t1, cal_weeks)
            te = frame.iloc[split.test_idx]
            te = te[np.isfinite(te[target_col(h)].to_numpy(dtype="float64"))]
            if te.empty:
                continue
            pred = fc.predict_horizon(te.reset_index(drop=True), h)
            part = pd.DataFrame(
                {
                    "fold": fold,
                    "horizon": h,
                    "business_id": te["business_id"].to_numpy(),
                    "currency": te["currency"].to_numpy(),
                    "as_of": te["as_of"].to_numpy(),
                    "y": te[target_col(h)].to_numpy(dtype="float64"),
                    "lgbm": pred["point"].to_numpy(),
                    "lower": pred["lower"].to_numpy(),
                    "upper": pred["upper"].to_numpy(),
                    "model_version": fc.version,
                }
            )
            for name, fn in baselines.items():
                part[name] = fn(te.reset_index(drop=True), h)
            parts.append(part)
    rows = pd.concat(parts, ignore_index=True)
    return summarise(rows, list(baselines), coverage_target), rows


def summarise(rows: pd.DataFrame, baseline_names: Sequence[str], coverage_target: float) -> pd.DataFrame:
    out: list[dict[str, object]] = []
    for h, g_h in rows.groupby("horizon"):
        groups: list[tuple[str, pd.DataFrame]] = [("ALL", g_h)]
        groups += [(str(c), g) for c, g in g_h.groupby("currency")]
        for cur, g in groups:
            y = g["y"].to_numpy(dtype="float64")
            lo, up = g["lower"].to_numpy(dtype="float64"), g["upper"].to_numpy(dtype="float64")
            rec: dict[str, object] = {
                "horizon": h,
                "currency": cur,
                "n": len(g),
                "wape_lgbm": wape(y, g["lgbm"].to_numpy(dtype="float64")),
            }
            for b in baseline_names:
                rec[f"wape_{b}"] = wape(y, g[b].to_numpy(dtype="float64"))
            rec.update(
                coverage=coverage(y, lo, up),
                target=coverage_target,
                mean_width=mean_width(lo, up),
                pinball=interval_pinball(y, lo, up, coverage_target),
            )
            out.append(rec)
    return pd.DataFrame(out)
>>>>>>> b5b2d1a (Day 4: LightGBM direct forecaster with split conformal intervals)

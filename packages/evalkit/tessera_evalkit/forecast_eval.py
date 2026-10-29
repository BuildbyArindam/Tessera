"""Summarise backtest results and write the baseline results table.

    python -m tessera_evalkit.forecast_eval --data data/synthetic --out docs/baseline_results.csv
"""

from __future__ import annotations

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

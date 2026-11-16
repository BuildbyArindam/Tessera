"""Held-out forecast evaluation: LightGBM + conformal against naive and seasonal-naive.

Fairness: the baselines have no intervals of their own, so each gets the SAME split-conformal treatment as the
LightGBM model: absolute residuals on the same calibration rows, one quantile per currency (Mondrian, global
fallback), 80% target. Intervals therefore differ only through the point forecast. Without this the baselines could
not be scored on coverage, width or pinball loss at all.

Metrics (per horizon x currency x model, pooled over test origins; the "ALL" row is the unweighted MACRO-average of
the per-currency values, because pooling raw amounts across currencies would let INR payroll dominate every number):
  WAPE      sum|y - yhat| / sum|y|          bias  sum(yhat - y) / sum|y|  (relative; positive = over-forecast)
  coverage  share of y inside [lower, upper] mean width  mean(upper - lower)
  pinball   mean of the pinball loss of the two interval endpoints at levels 0.1 and 0.9
Confidence intervals: cluster bootstrap over BUSINESSES (rows of one business are not independent, and origins one
week apart share most of their label window, so a row-level bootstrap would be far too narrow).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from tessera_core.forecast.conformal import SplitConformal
from tessera_core.forecast.direct_lgbm import DirectForecaster, target_col
from tessera_core.forecast.splits import make_split

from tessera_evalkit.eval_stats import cluster_weights, percentile_interval

FloatArray = NDArray[np.float64]
METRICS = ("wape", "bias", "coverage", "mean_width", "rel_width", "pinball")


def _pin(y: FloatArray, q: FloatArray, tau: float) -> FloatArray:
    d = y - q
    return np.asarray(np.maximum(tau * d, (tau - 1.0) * d))


def predict_all(
    frame: pd.DataFrame,
    horizons: Sequence[int],
    feature_cols: Sequence[str],
    baseline_cols: Mapping[str, str],
    test_start: pd.Timestamp,
    test_end: pd.Timestamp,
    cal_weeks: int,
    coverage: float,
    seed: int,
) -> tuple[pd.DataFrame, DirectForecaster]:
    """Long frame, one row per (model, horizon, test row): y, point, lower, upper. Returns (rows, fitted model)."""
    frame = frame.reset_index(drop=True)
    model = DirectForecaster(horizons, feature_cols, coverage, seed).fit(frame, test_start, cal_weeks)
    out: list[pd.DataFrame] = []
    for h in horizons:
        split = make_split(frame["as_of"], h, test_start, test_end, cal_weeks)
        y_all = frame[target_col(h)].to_numpy(dtype="float64")
        cal = split.cal_idx[np.isfinite(y_all[split.cal_idx])]
        te = split.test_idx[np.isfinite(y_all[split.test_idx])]
        if te.size == 0:
            continue
        test_rows = frame.iloc[te].reset_index(drop=True)
        base = {
            "business_id": test_rows["business_id"].to_numpy(),
            "currency": test_rows["currency"].to_numpy(),
            "as_of": test_rows["as_of"].to_numpy(),
            "horizon": h,
            "y": y_all[te],
        }
        p = model.predict_horizon(test_rows, h)
        out.append(
            pd.DataFrame(
                {**base, "model": "lightgbm_conformal", "point": p["point"], "lower": p["lower"], "upper": p["upper"]}
            )
        )
        cal_cur = frame["currency"].astype(str).iloc[cal].tolist()
        te_cur = test_rows["currency"].astype(str).tolist()
        for name, template in baseline_cols.items():
            col = template.format(h=h)
            cp = SplitConformal(coverage).calibrate(y_all[cal], frame[col].to_numpy(dtype="float64")[cal], cal_cur)
            pt = test_rows[col].to_numpy(dtype="float64")
            lo, up = cp.interval(pt, te_cur)
            out.append(pd.DataFrame({**base, "model": f"{name}_conformal", "point": pt, "lower": lo, "upper": up}))
    return pd.concat(out, ignore_index=True), model


def _business_sums(g: pd.DataFrame, businesses: list[str], coverage: float) -> FloatArray:
    """(n_businesses, 7) sums of |e|, e, |y|, hit, width, pinball, count, in the order of `businesses` (0 if absent)."""
    y, pt, lo, up = (g[c].to_numpy(dtype="float64") for c in ("y", "point", "lower", "upper"))
    a = (1.0 - coverage) / 2.0
    pin = 0.5 * (_pin(y, lo, a) + _pin(y, up, 1.0 - a))
    cols = (np.abs(y - pt), pt - y, np.abs(y), ((y >= lo) & (y <= up)).astype(float), up - lo, pin, np.ones_like(y))
    out = np.zeros((len(businesses), len(cols)), dtype=np.float64)
    index = {b: i for i, b in enumerate(businesses)}
    bid = g["business_id"].to_numpy()
    for j, col in enumerate(cols):
        for b in np.unique(bid):
            out[index[str(b)], j] = col[bid == b].sum()
    return out


# columns of the per-business sums matrix
_ABSE, _ERR, _ABSY, _HIT, _WID, _PIN, _N = range(7)
_FORMULAS: dict[str, tuple[tuple[int, ...], tuple[int, ...]]] = {
    "wape": ((_ABSE,), (_ABSY,)),
    "bias": ((_ERR,), (_ABSY,)),
    "coverage": ((_HIT,), (_N,)),
    "mean_width": ((_WID,), (_N,)),
    "rel_width": ((_WID,), (_ABSY,)),
    "pinball": ((_PIN,), (_N,)),
}


def _draws(sums: FloatArray, w: FloatArray, metric: str) -> tuple[float, FloatArray]:
    """Point value and bootstrap draws of one ratio metric from per-business sums and resample weights."""
    (n,), (d,) = _FORMULAS[metric]
    den = float(sums[:, d].sum())
    point = float(sums[:, n].sum()) / den if den else float("nan")
    with np.errstate(divide="ignore", invalid="ignore"):
        return point, (w @ sums[:, n]) / (w @ sums[:, d])


def _per_currency(g_h: pd.DataFrame, businesses: list[str], coverage: float) -> dict[tuple[str, str], FloatArray]:
    out: dict[tuple[str, str], FloatArray] = {}
    for (cur, model), g in g_h.groupby(["currency", "model"], sort=True):
        out[(str(cur), str(model))] = _business_sums(g, businesses, coverage)
    return out


def summarise(rows: pd.DataFrame, coverage: float, n_boot: int, seed: int) -> pd.DataFrame:
    """One row per (horizon, currency incl. ALL, model): each metric with a 95% business-bootstrap interval.

    All cells of a horizon share the same business resamples, so models and currencies are compared on identical draws.
    """
    rng = np.random.default_rng(seed)
    out: list[dict[str, object]] = []
    for h, g_h in rows.groupby("horizon", sort=True):
        businesses = sorted(g_h["business_id"].astype(str).unique())
        w = cluster_weights(len(businesses), n_boot, rng)
        sums = _per_currency(g_h, businesses, coverage)
        currencies = sorted({c for c, _ in sums})
        for model in sorted({m for _, m in sums}):
            per_cur = {c: sums[(c, model)] for c in currencies if (c, model) in sums}
            for cur in ["ALL", *currencies]:
                rec: dict[str, object] = {"horizon": h, "currency": cur, "model": model}
                if cur == "ALL":
                    rec["n_rows"] = int(sum(s[:, _N].sum() for s in per_cur.values()))
                    rec["n_businesses"] = len(businesses)
                else:
                    rec["n_rows"] = int(per_cur[cur][:, _N].sum())
                    rec["n_businesses"] = int((per_cur[cur][:, _N] > 0).sum())
                for metric in METRICS:
                    if cur == "ALL":
                        parts = [_draws(s, w, metric) for s in per_cur.values()]
                        point = float(np.nanmean([p for p, _ in parts]))
                        draws = np.nanmean(np.vstack([d for _, d in parts]), axis=0)
                    else:
                        point, draws = _draws(per_cur[cur], w, metric)
                    lo, hi = percentile_interval(draws)
                    rec[metric], rec[f"{metric}_lo"], rec[f"{metric}_hi"] = point, lo, hi
                out.append(rec)
    return pd.DataFrame(out)


def paired_wape(
    rows: pd.DataFrame, coverage: float, n_boot: int, seed: int, ref: str = "lightgbm_conformal"
) -> pd.DataFrame:
    """WAPE(ref) - WAPE(other) on shared business resamples. Negative means the reference model is more accurate."""
    rng = np.random.default_rng(seed + 1)
    out: list[dict[str, object]] = []
    for h, g_h in rows.groupby("horizon", sort=True):
        businesses = sorted(g_h["business_id"].astype(str).unique())
        w = cluster_weights(len(businesses), n_boot, rng)
        sums = _per_currency(g_h, businesses, coverage)
        currencies = sorted({c for c, _ in sums})
        for other in sorted({m for _, m in sums} - {ref}):
            pairs = {c: (_draws(sums[(c, ref)], w, "wape"), _draws(sums[(c, other)], w, "wape")) for c in currencies}
            for cur in ["ALL", *currencies]:
                chosen = list(pairs.values()) if cur == "ALL" else [pairs[cur]]
                point = float(np.nanmean([a[0] - b[0] for a, b in chosen]))
                draws = np.nanmean(np.vstack([a[1] - b[1] for a, b in chosen]), axis=0)
                lo, hi = percentile_interval(draws)
                out.append({"horizon": h, "currency": cur, "other": other, "wape_diff": point, "lo": lo, "hi": hi})
    return pd.DataFrame(out)


def forecast_table(
    model: DirectForecaster, frame: pd.DataFrame, horizons: Sequence[int], start: pd.Timestamp
) -> pd.DataFrame:
    """Forecasts at every origin on or after `start`, indexed (business, currency, as_of, horizon). Features only
    use data up to each origin, and the model was fitted without any row from `start` on."""
    rows = frame[frame["as_of"] >= start].reset_index(drop=True)
    parts = []
    for h in horizons:
        p = model.predict_horizon(rows, h)
        parts.append(
            pd.DataFrame(
                {
                    "business": rows["business_id"],
                    "currency": rows["currency"],
                    "as_of": rows["as_of"],
                    "h": h,
                    "point": p["point"],
                    "lower": p["lower"],
                    "upper": p["upper"],
                }
            )
        )
    return pd.concat(parts, ignore_index=True).set_index(["business", "currency", "as_of", "h"]).sort_index()

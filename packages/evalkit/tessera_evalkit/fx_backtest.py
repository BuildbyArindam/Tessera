"""Rolling-origin backtest of the FX range estimator.

At each origin the estimator sees ONLY rates up to that day (``rates[: i + 1]``), forecasts the
change over ``h`` calendar days, and is scored against the change that actually followed.

Caveat that goes in every report: origins are ``STRIDE`` business days apart but horizons are
longer, so outcomes overlap. The number of INDEPENDENT outcomes is roughly ``n_origins * stride /
steps``; ``n_eff`` is reported and the standard error uses it. Long horizons on ~2.5 years of data
have very few independent outcomes, so their coverage numbers are indicative, not proof.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from tessera_core.risk.fx_range import QUANTILE_LEVELS, estimate_fx_range, horizon_steps

from .risk_metrics import quantile_coverage

PAIRS = ("USDINR", "EURINR", "GBPINR", "SGDINR", "AUDINR", "EURUSD")
HORIZONS = (7, 30, 60, 90)
STRIDE = 5
MIN_HISTORY = 120
DROP_PCT = 2.0


MAX_MISSING_BDAYS = 5  # longer holes are data problems, not holidays


class FxDataGapError(ValueError):
    """A loaded rate series has a hole too long to be a holiday; returns across it would be wrong."""


def series_from_frame(series: pd.Series) -> pd.Series:
    """Drop NaN and verify there is no hole longer than ``MAX_MISSING_BDAYS`` business days."""
    clean = series.dropna().sort_index()
    if len(clean) < 2:
        return clean
    days = clean.index.to_numpy(dtype="datetime64[D]")
    missing = np.busday_count(days[:-1], days[1:]) - 1
    if (missing > MAX_MISSING_BDAYS).any():
        k = int(np.argmax(missing))
        raise FxDataGapError(
            f"{series.name}: {int(missing[k])} business days missing between {days[k]} and {days[k + 1]}"
        )
    return clean


def load_pair_series(fx_parquet: str | Path, pair: str) -> pd.Series:
    """Observed business-day rate of ``pair`` ("USDINR" = INR per 1 USD), oldest first."""
    if len(pair) != 6:
        raise ValueError(f"pair must look like 'USDINR', got {pair!r}")
    base, quote = pair[:3], pair[3:]
    df = pd.read_parquet(fx_parquet)
    obs = df[df["is_observed"]]
    wide = obs.pivot(index="date", columns="quote", values="rate")
    wide["EUR"] = 1.0
    for ccy in (base, quote):
        if ccy not in wide.columns:
            raise ValueError(f"currency {ccy} is not in {sorted(wide.columns)}")
    series = (wide[quote] / wide[base]).sort_index()
    series.index = pd.to_datetime(series.index)
    return series_from_frame(series.rename(pair))


def backtest_pair(series: pd.Series, horizon_days: int, *, n_sims: int = 4000, seed: int = 0) -> pd.DataFrame:
    steps = horizon_steps(horizon_days)
    rates = series.to_numpy(dtype=float)
    rows: list[dict[str, float | str | pd.Timestamp]] = []
    for i in range(MIN_HISTORY, len(rates) - steps, STRIDE):
        fr = estimate_fx_range(
            rates[: i + 1],
            pair=str(series.name),
            horizon_days=horizon_days,
            drop_thresholds_pct=(DROP_PCT,),
            n_sims=n_sims,
            seed=seed + i,
        )
        row: dict[str, float | str | pd.Timestamp] = {
            "pair": str(series.name),
            "horizon_days": horizon_days,
            "origin": series.index[i],
            "realized_pct": 100.0 * (rates[i + steps] / rates[i] - 1.0),
            "p_fall": fr.p_fall[0].probability,
        }
        for qp in fr.quantiles:
            row[f"q{qp.level:02d}"] = qp.change_pct
        rows.append(row)
    return pd.DataFrame(rows)


def run_backtest(fx_parquet: str | Path, *, n_sims: int = 4000, seed: int = 0) -> pd.DataFrame:
    frames = [
        backtest_pair(load_pair_series(fx_parquet, p), h, n_sims=n_sims, seed=seed) for p in PAIRS for h in HORIZONS
    ]
    return pd.concat(frames, ignore_index=True)


def coverage_by_pair(rows: pd.DataFrame) -> pd.DataFrame:
    """Per pair and horizon: outcomes below q05 / q50 / q95, and the path's mean realised change.

    The estimator is centred on zero drift on purpose. On one sample path with a strong trend, the
    pooled table mostly measures that trend; compare ``mean_realised_pct`` with the q50 column.
    """
    out = []
    for (pair, h), g in rows.groupby(["pair", "horizon_days"]):
        out.append(
            {
                "pair": pair,
                "horizon_days": int(h),
                "n_origins": len(g),
                "below_q05": float((g["realized_pct"] <= g["q05"]).mean()),
                "below_q50": float((g["realized_pct"] <= g["q50"]).mean()),
                "below_q95": float((g["realized_pct"] <= g["q95"]).mean()),
                "mean_realised_pct": float(g["realized_pct"].mean()),
            }
        )
    return pd.DataFrame(out)


def coverage_summary(rows: pd.DataFrame) -> pd.DataFrame:
    """Per horizon (pooled over pairs): coverage at each level, central-interval hits, n and n_eff."""
    out = []
    qcols = [f"q{lv:02d}" for lv in QUANTILE_LEVELS]
    for h, g in rows.groupby("horizon_days"):
        steps = horizon_steps(int(h))
        n_eff = max(1.0, len(g) * STRIDE / steps)
        cov = quantile_coverage(g["realized_pct"], g[qcols].to_numpy(), QUANTILE_LEVELS)
        for _, c in cov.iterrows():
            out.append(
                {
                    "horizon_days": int(h),
                    "level": int(c["level"]),
                    "nominal": c["nominal"],
                    "observed": c["observed"],
                    "gap": c["gap"],
                    "se_eff": float(np.sqrt(c["nominal"] * (1 - c["nominal"]) / n_eff)),
                    "n_origins": len(g),
                    "n_eff": n_eff,
                }
            )
        for lo, hi in ((5, 95), (25, 75)):
            hit = (g["realized_pct"] >= g[f"q{lo:02d}"]) & (g["realized_pct"] <= g[f"q{hi:02d}"])
            nominal = (hi - lo) / 100.0
            out.append(
                {
                    "horizon_days": int(h),
                    "level": f"{lo}-{hi}",
                    "nominal": nominal,
                    "observed": float(hit.mean()),
                    "gap": float(hit.mean()) - nominal,
                    "se_eff": float(np.sqrt(nominal * (1 - nominal) / n_eff)),
                    "n_origins": len(g),
                    "n_eff": n_eff,
                }
            )
    return pd.DataFrame(out)

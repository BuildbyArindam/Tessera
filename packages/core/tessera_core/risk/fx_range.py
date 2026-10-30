"""Read-only tool: probabilistic range for an FX rate over a horizon (filtered historical simulation).

Method
    1. Daily log returns r_t of the pair, observed business days only.
    2. EWMA variance (RiskMetrics): s2_t = lam * s2_{t-1} + (1 - lam) * r_{t-1}^2, so the volatility
       used for r_t depends only on returns BEFORE t.
    3. Standardise: z_t = r_t / s_t, then re-centre to mean 0 and rescale to unit variance. This
       drops any historical drift: the range is centred on "no expected change" by design.
    4. Bootstrap: for each simulated path draw z with replacement and roll the EWMA variance forward
       with the simulated return, so a shock early in the path widens the days after it
       (volatility clustering), instead of scaling one-day vol by sqrt(h).
    5. Read quantiles of exp(sum of log returns) - 1 and P(change < -x%).

Horizon: ``horizon_days`` is CALENDAR days; it is converted to business-day steps with 252/365.
Measured behaviour (GARCH(1,1) with t(6) shocks, 8 independent paths, non-overlapping horizons):
the median is unbiased, but the tails are slightly thin: the nominal 5-95 interval realised about
88% at 7 days and 87% at 30 days. Treat the 5% / 95% quantiles as mildly optimistic.
Direction: "falls" means the quoted pair (quote units per 1 base unit) goes down. For the
opposite exposure (a payable in the quote currency) query the inverse pair.
Randomness is a seeded ``numpy.random.Generator``: same inputs and seed give identical output.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

from tessera_core.domain.base import DomainModel

QUANTILE_LEVELS = (5, 25, 50, 75, 95)
DEFAULT_DROP_THRESHOLDS_PCT = (1.0, 2.0, 5.0)
DEFAULT_LAMBDA = 0.94
BURN_IN = 30
MIN_OBSERVATIONS = BURN_IN + 30
TRADING_DAYS = 252
VAR_FLOOR = 1e-12  # keeps a flat early history from dividing by zero; real FX daily variance is ~1e-5
FloatArray = npt.NDArray[np.float64]


class InsufficientHistoryError(ValueError):
    """Not enough clean rate history to estimate a range."""


class InvalidRatesError(ValueError):
    """Rates contain NaN, infinity, a non-positive value, or are not a 1-D sequence (e.g. an unfilled gap)."""


class QuantilePoint(DomainModel):
    level: int  # percentile, one of QUANTILE_LEVELS
    value: float  # the pair rate at this quantile (a ratio, not money)
    change_pct: float


class DropProbability(DomainModel):
    threshold_pct: float  # P(rate change < -threshold_pct %)
    probability: float


class FxRange(DomainModel):
    pair: str
    horizon_days: int
    steps: int
    spot: float
    n_observations: int
    daily_vol_pct: float  # current EWMA one-day volatility, in percent
    quantiles: tuple[QuantilePoint, ...]
    p_fall: tuple[DropProbability, ...]
    n_sims: int
    seed: int


def horizon_steps(horizon_days: int) -> int:
    """Business-day steps for a calendar horizon (at least 1)."""
    if horizon_days < 1:
        raise ValueError("horizon_days must be >= 1")
    return max(1, round(horizon_days * TRADING_DAYS / 365))


def _log_returns(rates: npt.ArrayLike) -> tuple[FloatArray, float]:
    r = np.asarray(rates, dtype=float)
    if r.ndim != 1:
        raise InvalidRatesError("rates must be a 1-D sequence")
    if not np.isfinite(r).all() or (r <= 0).any():
        raise InvalidRatesError("rates must be finite and strictly positive (NaN or a gap in the series?)")
    if len(r) < MIN_OBSERVATIONS + 1:
        raise InsufficientHistoryError(f"need at least {MIN_OBSERVATIONS + 1} rates, got {len(r)}")
    return np.diff(np.log(r)).astype(float), float(r[-1])


def ewma_pre_variance(returns: FloatArray, lam: float) -> tuple[FloatArray, float]:
    """Variance forecast for each return made BEFORE seeing it, plus the forecast for the next one.

    Seeded with the mean square of the first ``BURN_IN`` returns.
    """
    s2 = np.empty(len(returns))
    cur = max(float(np.mean(returns[:BURN_IN] ** 2)), VAR_FLOOR)
    for i, r in enumerate(returns):
        s2[i] = cur
        cur = max(lam * cur + (1.0 - lam) * float(r) ** 2, VAR_FLOOR)
    return s2, cur


def standardised_residuals(returns: FloatArray, lam: float) -> tuple[FloatArray, float]:
    """Centred, unit-variance z_t = r_t / s_t (after burn-in) and the next-day variance forecast."""
    s2, next_var = ewma_pre_variance(returns, lam)
    z = returns[BURN_IN:] / np.sqrt(s2[BURN_IN:])
    z = z - z.mean()
    sd = float(z.std())
    if not math.isfinite(sd) or sd <= 0:
        raise InsufficientHistoryError("rate history has no variation; cannot standardise returns")
    return (z / sd).astype(float), next_var


def simulate_changes(
    z: FloatArray, next_var: float, steps: int, lam: float, n_sims: int, rng: np.random.Generator
) -> FloatArray:
    """Simulated fractional rate change (exp(sum of log returns) - 1) per path."""
    var = np.full(n_sims, next_var)
    cum = np.zeros(n_sims)
    for _ in range(steps):
        draw = z[rng.integers(0, len(z), size=n_sims)]
        ret = np.sqrt(var) * draw
        cum += ret
        var = lam * var + (1.0 - lam) * ret**2
    return np.expm1(cum)


def estimate_fx_range(
    rates: Sequence[float] | npt.NDArray[np.float64],
    *,
    pair: str,
    horizon_days: int,
    drop_thresholds_pct: Sequence[float] = DEFAULT_DROP_THRESHOLDS_PCT,
    lam: float = DEFAULT_LAMBDA,
    n_sims: int = 20_000,
    seed: int = 0,
) -> FxRange:
    """Distribution of the ``pair`` rate ``horizon_days`` ahead.

    ``rates`` are consecutive observed business-day rates, oldest first, ending at the as-of date.
    """
    if not 0.0 < lam < 1.0:
        raise ValueError("lam must be in (0, 1)")
    if n_sims < 1000:
        raise ValueError("n_sims must be at least 1000")
    thresholds = tuple(float(x) for x in drop_thresholds_pct)
    if any(not 0.0 < x < 100.0 for x in thresholds):
        raise ValueError("drop thresholds are percentages in (0, 100)")
    steps = horizon_steps(horizon_days)
    returns, spot = _log_returns(rates)
    z, next_var = standardised_residuals(returns, lam)
    changes = simulate_changes(z, next_var, steps, lam, n_sims, np.random.default_rng(seed))
    qs = np.quantile(changes, [q / 100.0 for q in QUANTILE_LEVELS])
    return FxRange(
        pair=pair,
        horizon_days=horizon_days,
        steps=steps,
        spot=spot,
        n_observations=len(returns) + 1,
        daily_vol_pct=100.0 * math.sqrt(next_var),
        quantiles=tuple(
            QuantilePoint(level=lv, value=spot * (1.0 + float(c)), change_pct=100.0 * float(c))
            for lv, c in zip(QUANTILE_LEVELS, qs, strict=True)
        ),
        p_fall=tuple(
            DropProbability(threshold_pct=x, probability=float(np.mean(changes < -x / 100.0))) for x in thresholds
        ),
        n_sims=n_sims,
        seed=seed,
    )

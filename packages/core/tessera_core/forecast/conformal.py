"""Hand-written split conformal prediction (absolute-residual scores).

The whole method in four lines:
  1. Fit the model on TRAIN rows.
  2. On a held-out, later CALIBRATION window compute scores s_i = |y_i - f(x_i)|.
  3. Take q = the ceil((n+1) * coverage)-th smallest score (finite-sample correction).
  4. Predict the interval [f(x) - q, f(x) + q].

Guarantee: if calibration and test rows are exchangeable, P(y in interval) >= coverage.
Time series are only approximately exchangeable, so under drift the guarantee degrades;
that is why the Day 14 autonomy dial monitors empirical coverage and demotes on collapse.

Optional Mondrian grouping (e.g. by currency) gives one q per group, falling back to the
global q for groups with too few calibration rows.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]


def conformal_quantile(scores: FloatArray, coverage: float) -> float:
    """Finite-sample-corrected quantile of nonconformity scores."""
    if not 0.0 < coverage < 1.0:
        raise ValueError(f"coverage must be in (0, 1), got {coverage}")
    n = int(scores.size)
    k = math.ceil((n + 1) * coverage)
    if n == 0 or k > n:
        raise ValueError(
            f"need at least {math.ceil(coverage / (1 - coverage))} calibration scores for coverage={coverage}, got {n}"
        )
    if not np.all(np.isfinite(scores)):
        raise ValueError("calibration scores must be finite")
    return float(np.sort(scores)[k - 1])


class SplitConformal:
    """Symmetric split-conformal intervals around a point forecast."""

    def __init__(self, coverage: float = 0.8, min_group_size: int = 30) -> None:
        if not 0.0 < coverage < 1.0:
            raise ValueError(f"coverage must be in (0, 1), got {coverage}")
        self.coverage = coverage
        self.min_group_size = min_group_size
        self.q_global: float | None = None
        self.q_by_group: dict[str, float] = {}
        self.n_calibration: int = 0

    def calibrate(
        self,
        y_true: FloatArray,
        y_pred: FloatArray,
        groups: Sequence[str] | None = None,
    ) -> SplitConformal:
        """Compute quantile(s) from calibration residuals. Must never see test rows."""
        if y_true.shape != y_pred.shape:
            raise ValueError("y_true and y_pred must have the same shape")
        scores = np.abs(y_true - y_pred)
        self.n_calibration = int(scores.size)
        self.q_global = conformal_quantile(scores, self.coverage)
        self.q_by_group = {}
        if groups is not None:
            if len(groups) != scores.size:
                raise ValueError("groups must align with y_true")
            g = np.asarray(groups)
            for key in np.unique(g):
                s = scores[g == key]
                if s.size >= self.min_group_size:
                    try:
                        self.q_by_group[str(key)] = conformal_quantile(s, self.coverage)
                    except ValueError:
                        continue  # group too small for this coverage: use global q
        return self

    def half_width(self, n: int, groups: Sequence[str] | None = None) -> FloatArray:
        """Per-row interval half-width."""
        if self.q_global is None:
            raise RuntimeError("calibrate() must be called before predicting intervals")
        q = np.full(n, self.q_global, dtype=np.float64)
        if groups is not None:
            if len(groups) != n:
                raise ValueError("groups must align with predictions")
            for i, key in enumerate(groups):
                q[i] = self.q_by_group.get(str(key), self.q_global)
        return q

    def interval(self, y_pred: FloatArray, groups: Sequence[str] | None = None) -> tuple[FloatArray, FloatArray]:
        w = self.half_width(int(y_pred.size), groups)
        return y_pred - w, y_pred + w

    def state(self) -> dict[str, Any]:
        return {
            "coverage": self.coverage,
            "min_group_size": self.min_group_size,
            "q_global": self.q_global,
            "q_by_group": dict(sorted(self.q_by_group.items())),
            "n_calibration": self.n_calibration,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> SplitConformal:
        obj = cls(float(state["coverage"]), int(state["min_group_size"]))
        q = state["q_global"]
        obj.q_global = None if q is None else float(q)
        obj.q_by_group = {str(k): float(v) for k, v in state["q_by_group"].items()}
        obj.n_calibration = int(state["n_calibration"])
        return obj

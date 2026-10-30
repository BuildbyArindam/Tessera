"""Direct multi-horizon LightGBM forecaster with split-conformal intervals.

Input contract (what Day 3's feature pipeline must hand over):
  a DataFrame with one row per (business_id, currency, as_of) origin containing
    - key columns: business_id, currency, as_of (datetime64)
    - feature columns built ONLY from data with timestamp <= as_of
    - one label column per horizon: y_h{h}  (net cash flow realised h weeks after as_of;
      NaN when the label is not yet observable)

Strategy: one model per horizon (direct), trained on TRAIN rows only; hyperparameters
chosen by a small expanding-window search INSIDE the train rows; conformal quantiles come
from a later, disjoint calibration window. Test rows are never touched by fit().
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

import lightgbm as lgb
import numpy as np
import pandas as pd
from numpy.typing import NDArray

from tessera_core.forecast.conformal import SplitConformal
from tessera_core.forecast.results import ForecastResult, to_decimal
from tessera_core.forecast.splits import make_split

FloatArray = NDArray[np.float64]
KEYS = ("business_id", "currency", "as_of")
CURRENCY_COL = "_currency_code"

DEFAULT_GRID: tuple[dict[str, Any], ...] = (
    {"num_leaves": 7, "learning_rate": 0.05, "n_estimators": 150, "min_child_samples": 20},
    {"num_leaves": 15, "learning_rate": 0.05, "n_estimators": 150, "min_child_samples": 20},
    {"num_leaves": 15, "learning_rate": 0.03, "n_estimators": 300, "min_child_samples": 40},
    {"num_leaves": 31, "learning_rate": 0.03, "n_estimators": 200, "min_child_samples": 40},
    {"num_leaves": 7, "learning_rate": 0.1, "n_estimators": 80, "min_child_samples": 10},
    {"num_leaves": 31, "learning_rate": 0.05, "n_estimators": 100, "min_child_samples": 60},
)


def target_col(horizon_weeks: int) -> str:
    return f"y_h{horizon_weeks}"


class DirectForecaster:
    def __init__(
        self,
        horizons: Sequence[int],
        feature_cols: Sequence[str],
        coverage: float = 0.8,
        seed: int = 0,
        grid: Sequence[Mapping[str, Any]] = DEFAULT_GRID,
        use_currency: bool = True,
        n_inner_folds: int = 2,
        min_group_size: int = 30,
    ) -> None:
        self.horizons = tuple(horizons)
        self.feature_cols = tuple(feature_cols)
        self.coverage = coverage
        self.seed = seed
        self.grid = tuple(dict(g) for g in grid)
        self.use_currency = use_currency
        self.n_inner_folds = n_inner_folds
        self.min_group_size = min_group_size
        self.currency_codes: dict[str, int] = {}
        self.models: dict[int, lgb.Booster] = {}
        self.conformal: dict[int, SplitConformal] = {}
        self.best_params: dict[int, dict[str, Any]] = {}
        self.search_scores: dict[int, list[float]] = {}
        self.version: str | None = None

    # ---- data plumbing -------------------------------------------------
    def cols_for(self, h: int) -> list[str]:
        """Feature names for horizon h. A name containing "{h}" (e.g. "seasonal_lag_h{h}") is
        expanded per horizon, so direct models can use horizon-aligned features such as the
        value observed at as_of + h - 52 weeks (same week last year)."""
        return [c.format(h=h) for c in self.feature_cols]

    def _check(self, frame: pd.DataFrame, need_targets: bool) -> None:
        needed = [c for h in self.horizons for c in self.cols_for(h)]
        missing = [c for c in (*KEYS, *dict.fromkeys(needed)) if c not in frame.columns]
        if need_targets:
            missing += [target_col(h) for h in self.horizons if target_col(h) not in frame.columns]
        if missing:
            raise KeyError(f"frame is missing columns: {missing}")

    def _matrix(self, frame: pd.DataFrame, h: int) -> pd.DataFrame:
        x = frame[self.cols_for(h)].astype("float64").reset_index(drop=True)
        if self.use_currency:
            codes = frame["currency"].map(self.currency_codes).astype("float64")
            x[CURRENCY_COL] = codes.to_numpy()  # unseen currency -> NaN
        return x

    def _params(self, base: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **base,
            "objective": "l1",
            "random_state": self.seed,
            "n_jobs": 1,
            "deterministic": True,
            "force_row_wise": True,
            "verbose": -1,
        }

    def _fit_one(self, x: pd.DataFrame, y: FloatArray, base: Mapping[str, Any]) -> lgb.Booster:
        cat: list[str] | Literal["auto"] = [CURRENCY_COL] if self.use_currency else "auto"
        reg = lgb.LGBMRegressor(**self._params(base))
        reg.fit(x, y, categorical_feature=cat)
        return reg.booster_

    # ---- hyperparameter search (inside TRAIN rows only) -----------------
    def _search(self, x: pd.DataFrame, y: FloatArray, t: pd.Series, h: int) -> tuple[dict[str, Any], list[float]]:
        dates = np.sort(t.unique())
        chunks = np.array_split(dates, self.n_inner_folds + 1)
        lab = t + pd.Timedelta(weeks=h)
        scores = [0.0] * len(self.grid)
        used = 0
        for k in range(1, len(chunks)):
            val_start = chunks[k][0]
            val_end = chunks[k][-1]
            tr = (lab < val_start).to_numpy()
            va = ((t >= val_start) & (t <= val_end)).to_numpy()
            if tr.sum() < 50 or va.sum() < 10:
                continue
            used += 1
            for i, params in enumerate(self.grid):
                booster = self._fit_one(x[tr], y[tr], params)
                scores[i] += float(np.mean(np.abs(y[va] - booster.predict(x[va]))))
        if used == 0:
            return self.grid[0], scores
        return self.grid[int(np.argmin(scores))], [s / used for s in scores]

    # ---- fit / calibrate -------------------------------------------------
    def fit(self, frame: pd.DataFrame, test_start: pd.Timestamp, cal_weeks: int) -> DirectForecaster:
        """Train on rows whose labels are realised before the calibration window, calibrate on
        the window between, and ignore everything from `test_start` onwards."""
        from tessera_core.forecast.artifacts import compute_version  # local: avoids import cycle

        self._check(frame, need_targets=True)
        frame = frame.reset_index(drop=True)
        self.currency_codes = {c: i for i, c in enumerate(sorted(frame["currency"].astype(str).unique()))}
        t = pd.to_datetime(frame["as_of"])
        for h in self.horizons:
            x_all = self._matrix(frame, h)
            split = make_split(t, h, test_start, test_start + pd.Timedelta(weeks=1), cal_weeks)
            y_all = frame[target_col(h)].to_numpy(dtype="float64")
            ok_tr = split.train_idx[np.isfinite(y_all[split.train_idx])]
            ok_cal = split.cal_idx[np.isfinite(y_all[split.cal_idx])]
            if ok_tr.size == 0 or ok_cal.size == 0:
                raise ValueError(f"horizon {h}: empty train ({ok_tr.size}) or calibration ({ok_cal.size}) set")
            x_tr, y_tr, t_tr = (
                x_all.iloc[ok_tr].reset_index(drop=True),
                y_all[ok_tr],
                t.iloc[ok_tr].reset_index(drop=True),
            )
            best, scores = self._search(x_tr, y_tr, t_tr, h)
            self.best_params[h], self.search_scores[h] = best, scores
            self.models[h] = self._fit_one(x_tr, y_tr, best)
            pred_cal = self.models[h].predict(x_all.iloc[ok_cal])
            groups = frame["currency"].astype(str).iloc[ok_cal].tolist() if self.use_currency else None
            cp = SplitConformal(self.coverage, self.min_group_size)
            self.conformal[h] = cp.calibrate(y_all[ok_cal], np.asarray(pred_cal, dtype=np.float64), groups)
        self.version = compute_version(self)
        return self

    # ---- prediction --------------------------------------------------------
    def predict_horizon(self, frame: pd.DataFrame, h: int) -> pd.DataFrame:
        if h not in self.models:
            raise KeyError(f"no model for horizon {h}; fitted: {sorted(self.models)}")
        self._check(frame, need_targets=False)
        point = np.asarray(self.models[h].predict(self._matrix(frame, h)), dtype=np.float64)
        groups = frame["currency"].astype(str).tolist() if self.use_currency else None
        lower, upper = self.conformal[h].interval(point, groups)
        return pd.DataFrame({"point": point, "lower": lower, "upper": upper}, index=frame.index)

    def forecast(self, frame: pd.DataFrame) -> list[ForecastResult]:
        """Typed results for every row and horizon (for the API / planner)."""
        if self.version is None:
            raise RuntimeError("fit() or load() first")
        out: list[ForecastResult] = []
        for h in self.horizons:
            p = self.predict_horizon(frame, h)
            for (_, row), (_, r) in zip(frame.iterrows(), p.iterrows(), strict=True):
                out.append(
                    ForecastResult(
                        business_id=str(row["business_id"]),
                        currency=str(row["currency"]),
                        horizon_weeks=h,
                        point=to_decimal(float(r["point"])),
                        lower=to_decimal(float(r["lower"])),
                        upper=to_decimal(float(r["upper"])),
                        target_coverage=self.coverage,
                        model_version=self.version,
                    )
                )
        return out

"""Logistic regression and LightGBM late-payment classifiers with isotonic calibration.

Both models are trained on the TRAIN stage only. The isotonic map is fitted on the CALIBRATION
stage only (a later chronological window, see ``late_split``), never on training rows, because a
model's own training predictions are over-confident. Calibrated probabilities are clipped to
``[P_MIN, P_MAX]``: an isotonic fit can output exactly 0 or 1 on a small window, and the agent must
never be told an outcome is certain on the strength of a few hundred snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from tessera_core.risk.late_features import GBM_FEATURES, LR_FEATURES

ModelName = Literal["logreg", "lgbm"]
P_MIN, P_MAX = 0.01, 0.99
MIN_TRAIN_ROWS = 200
MIN_CAL_ROWS = 100
MIN_CAL_INVOICES = 30  # hard floor: below this an isotonic map is meaningless
WARN_CAL_INVOICES = 200  # judgment call, not derived: see the note attached in fit_late_scorer
FloatArray = npt.NDArray[np.float64]


class InsufficientDataError(ValueError):
    """Too few rows, or a single class, to train or calibrate honestly."""


def _check_stage(frame: pd.DataFrame, minimum: int, stage: str) -> None:
    if len(frame) < minimum:
        raise InsufficientDataError(f"{stage} stage has {len(frame)} rows, need at least {minimum}")
    if frame["y"].nunique() < 2:
        raise InsufficientDataError(f"{stage} stage contains a single class; cannot fit a probability model")
    # Snapshots of one invoice are strongly correlated, so independent evidence is the invoice count.
    if stage == "calibration" and "invoice_id" in frame and frame["invoice_id"].nunique() < MIN_CAL_INVOICES:
        raise InsufficientDataError(
            f"calibration stage covers {frame['invoice_id'].nunique()} invoices, need at least {MIN_CAL_INVOICES}"
        )


@dataclass(frozen=True)
class CalibratedModel:
    name: ModelName
    features: tuple[str, ...]
    estimator: Any
    calibrator: IsotonicRegression

    def predict_raw(self, x: pd.DataFrame) -> FloatArray:
        return np.asarray(self.estimator.predict_proba(x[list(self.features)])[:, 1], dtype=float)

    def predict_proba(self, x: pd.DataFrame) -> FloatArray:
        cal = np.asarray(self.calibrator.predict(self.predict_raw(x)), dtype=float)
        return np.asarray(np.clip(cal, P_MIN, P_MAX), dtype=float)


def _make_estimator(name: ModelName, seed: int) -> tuple[Any, tuple[str, ...]]:
    if name == "logreg":
        pipe = Pipeline(
            [
                ("impute", SimpleImputer(strategy="median")),
                ("scale", StandardScaler()),
                ("clf", LogisticRegression(C=1.0, max_iter=2000, random_state=seed)),
            ]
        )
        return pipe, tuple(LR_FEATURES)
    gbm = LGBMClassifier(
        n_estimators=250,
        learning_rate=0.03,
        num_leaves=15,
        min_child_samples=40,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        reg_lambda=2.0,
        random_state=seed,
        n_jobs=1,
        deterministic=True,
        force_row_wise=True,
        verbose=-1,
    )
    return gbm, tuple(GBM_FEATURES)


def fit_calibrated(name: ModelName, train: pd.DataFrame, cal: pd.DataFrame, seed: int = 0) -> CalibratedModel:
    """Fit on ``train``, calibrate on ``cal``. Both frames need ``y`` and the feature columns."""
    _check_stage(train, MIN_TRAIN_ROWS, "training")
    _check_stage(cal, MIN_CAL_ROWS, "calibration")
    est, feats = _make_estimator(name, seed)
    est.fit(train[list(feats)], train["y"].to_numpy(dtype=float))
    raw = np.asarray(est.predict_proba(cal[list(feats)])[:, 1], dtype=float)
    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    iso.fit(raw, cal["y"].to_numpy(dtype=float))
    return CalibratedModel(name, feats, est, iso)


@dataclass(frozen=True)
class LateScorer:
    """Both calibrated models plus provenance. ``default`` is used unless a caller overrides it."""

    models: dict[str, CalibratedModel]
    default: ModelName = "lgbm"
    trained_before: pd.Timestamp | None = None
    n_train: int = 0
    n_cal: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def predict(self, x: pd.DataFrame, model: ModelName | None = None) -> FloatArray:
        name = model or self.default
        if name not in self.models:
            raise KeyError(f"unknown model {name!r}; have {sorted(self.models)}")
        return self.models[name].predict_proba(x)


def fit_late_scorer(train: pd.DataFrame, cal: pd.DataFrame, seed: int = 0, default: ModelName = "lgbm") -> LateScorer:
    models = {n: fit_calibrated(n, train, cal, seed) for n in ("logreg", "lgbm")}
    notes: list[str] = []
    n_cal_inv = int(cal["invoice_id"].nunique()) if "invoice_id" in cal else None
    if n_cal_inv is not None and n_cal_inv < WARN_CAL_INVOICES:
        notes.append(
            f"calibration window has only {n_cal_inv} invoices (< {WARN_CAL_INVOICES}): isotonic calibration "
            "can overfit a small window and make held-out probabilities worse; check held-out ECE before trusting"
        )
    return LateScorer(
        models=models,
        default=default,
        trained_before=pd.Timestamp(cal["origin"].min()) if "origin" in cal else None,
        n_train=len(train),
        n_cal=len(cal),
        notes=tuple(notes),
    )

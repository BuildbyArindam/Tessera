"""Save / load fitted forecasters with a content-addressed version hash.

The version is sha256 over: feature list, horizons, coverage, chosen params, conformal
state, currency codes and every booster's text dump. Any change to any of those changes
the hash, so a ForecastResult.model_version pins the exact model that produced it
(audit replayability, master rule 8).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import lightgbm as lgb

from tessera_core.forecast.conformal import SplitConformal

if TYPE_CHECKING:
    from tessera_core.forecast.direct_lgbm import DirectForecaster

MANIFEST = "manifest.json"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _spec(fc: DirectForecaster) -> dict[str, Any]:
    return {
        "horizons": list(fc.horizons),
        "feature_cols": list(fc.feature_cols),
        "coverage": fc.coverage,
        "seed": fc.seed,
        "use_currency": fc.use_currency,
        "min_group_size": fc.min_group_size,
        "currency_codes": dict(sorted(fc.currency_codes.items())),
        "best_params": {str(h): fc.best_params[h] for h in sorted(fc.best_params)},
        "conformal": {str(h): fc.conformal[h].state() for h in sorted(fc.conformal)},
    }


def compute_version(fc: DirectForecaster) -> str:
    h = hashlib.sha256(_canonical(_spec(fc)).encode())
    for horizon in sorted(fc.models):
        h.update(fc.models[horizon].model_to_string().encode())
    return h.hexdigest()[:16]


def save(fc: DirectForecaster, directory: Path) -> Path:
    if fc.version is None:
        raise RuntimeError("cannot save an unfitted forecaster")
    directory.mkdir(parents=True, exist_ok=True)
    digests: dict[str, str] = {}
    for h, booster in fc.models.items():
        text = booster.model_to_string()
        (directory / f"model_h{h}.txt").write_text(text)
        digests[str(h)] = hashlib.sha256(text.encode()).hexdigest()
    manifest = {**_spec(fc), "version": fc.version, "model_sha256": digests}
    (directory / MANIFEST).write_text(_canonical(manifest))
    return directory


def load(directory: Path) -> DirectForecaster:
    """Load and verify: raises ValueError if the recomputed hash differs (tampered/corrupt)."""
    from tessera_core.forecast.direct_lgbm import DirectForecaster

    manifest = json.loads((directory / MANIFEST).read_text())
    fc = DirectForecaster(
        horizons=manifest["horizons"],
        feature_cols=manifest["feature_cols"],
        coverage=manifest["coverage"],
        seed=manifest["seed"],
        use_currency=manifest["use_currency"],
        min_group_size=manifest["min_group_size"],
    )
    fc.currency_codes = {str(k): int(v) for k, v in manifest["currency_codes"].items()}
    fc.best_params = {int(h): v for h, v in manifest["best_params"].items()}
    fc.conformal = {int(h): SplitConformal.from_state(s) for h, s in manifest["conformal"].items()}
    texts: dict[int, str] = {}
    for h in fc.horizons:
        text = (directory / f"model_h{h}.txt").read_text()
        # verify BEFORE parsing: LightGBM can hard-crash on corrupt model text
        if hashlib.sha256(text.encode()).hexdigest() != manifest["model_sha256"][str(h)]:
            raise ValueError(f"artefact hash mismatch: model_h{h}.txt was modified")
        texts[h] = text
    fc.models = {h: lgb.Booster(model_str=t) for h, t in texts.items()}
    fc.version = compute_version(fc)
    if fc.version != manifest["version"]:
        raise ValueError(f"artefact hash mismatch: stored {manifest['version']}, computed {fc.version}")
    return fc

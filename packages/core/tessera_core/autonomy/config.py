"""AutonomyConfig: every threshold the controller uses, loaded from YAML.

Same discipline as the policy rules: unknown keys are errors, duplicate YAML keys are errors, decimals must be quoted
strings (a float is rejected), and the config has a canonical hash so an audit event names the exact thresholds that
produced a decision. Nothing in the controller carries a number of its own.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from tessera_core.domain.base import DomainModel
from tessera_core.policy.context import AutonomyLevel
from tessera_core.policy.loader import strict_yaml_load
from tessera_core.policy.numeric import NonNegativeRatio, UnitRatio
from tessera_core.policy.ruleset import RulesetError

DEFAULT_CONFIG_PATH = Path(__file__).parent / "autonomy.yaml"
HASH_FORMAT_VERSION = 1


class AutonomyConfigError(ValueError):
    """The autonomy configuration is invalid. Raised at load time, never while assessing evidence."""


class WindowConfig(DomainModel):
    evidence_days: int = Field(strict=True, ge=1, le=365)
    drift_days: int = Field(strict=True, ge=1, le=365)


class PromotionConfig(DomainModel):
    max_level: Literal["L1", "L2", "L3"]
    min_shadow_decisions: int = Field(strict=True, ge=1, le=100_000)
    min_agreement: UnitRatio
    min_saving_observations: int = Field(strict=True, ge=2, le=100_000)
    min_saving_lower_bound: NonNegativeRatio
    min_coverage_observations: int = Field(strict=True, ge=1, le=100_000)
    coverage_tolerance: UnitRatio


class BootstrapConfig(DomainModel):
    resamples: int = Field(strict=True, ge=100, le=100_000)
    confidence: UnitRatio
    seed: int = Field(strict=True, ge=0, le=2**31 - 1)


class DemotionStep(DomainModel):
    levels_down: int = Field(strict=True, ge=1, le=len(AutonomyLevel) - 1)


class CollapseStep(DemotionStep):
    margin: UnitRatio
    min_observations: int = Field(strict=True, ge=1, le=100_000)


class DemotionConfig(DomainModel):
    on_violation: DemotionStep
    on_kill_switch: DemotionStep
    on_coverage_collapse: CollapseStep


class AutonomyConfig(DomainModel):
    window: WindowConfig
    promotion: PromotionConfig
    bootstrap: BootstrapConfig
    demotion: DemotionConfig

    @property
    def max_level(self) -> AutonomyLevel:
        return AutonomyLevel[self.promotion.max_level]

    @property
    def hash(self) -> str:
        """sha256 over the validated values in canonical form, so a comment edit does not change it but a limit does."""
        payload = {"format": HASH_FORMAT_VERSION, "config": self.model_dump(mode="json")}
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(blob.encode("ascii")).hexdigest()


def autonomy_config_from_text(text: str, source: str = "autonomy.yaml") -> AutonomyConfig:
    try:
        data = strict_yaml_load(text, source)
    except RulesetError as exc:
        raise AutonomyConfigError(str(exc)) from None
    if not isinstance(data, dict):
        raise AutonomyConfigError(f"{source}: the file must contain a single mapping")
    try:
        return AutonomyConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise AutonomyConfigError(f"{source}: {problems}") from None


def load_autonomy_config(path: Path = DEFAULT_CONFIG_PATH) -> AutonomyConfig:
    if not path.is_file():
        raise AutonomyConfigError(f"autonomy config not found: {path}")
    return autonomy_config_from_text(path.read_text(encoding="utf-8"), path.name)

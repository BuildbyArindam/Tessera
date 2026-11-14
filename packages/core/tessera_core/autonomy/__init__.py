"""tessera_core.autonomy: the autonomy dial (Day 14). Pure logic; thresholds come from autonomy.yaml.

Levels L0..L3 are `AutonomyLevel` (defined next to PolicyContext, which carries the current one). What each level may
do is the `autonomy_level_gate` policy rule's table. This package decides when the dial moves: `AutonomyController`
turns an evidence window into "demote now", "propose a promotion" (an ESCALATE: a person must confirm) or "stay".
"""

from tessera_core.autonomy.config import (
    DEFAULT_CONFIG_PATH,
    AutonomyConfig,
    AutonomyConfigError,
    autonomy_config_from_text,
    load_autonomy_config,
)
from tessera_core.autonomy.controller import (
    TRIGGER_COVERAGE_COLLAPSE,
    TRIGGER_KILL_SWITCH,
    TRIGGER_VIOLATION,
    Assessment,
    AutonomyController,
    Criterion,
    TransitionKind,
)
from tessera_core.autonomy.evidence import (
    AutonomyState,
    CoverageObservation,
    Evidence,
    SavingObservation,
    ShadowObservation,
    ViolationObservation,
    in_window,
)
from tessera_core.autonomy.shadow import AUTONOMY_GATE_RULE_ID, is_shadow_decision
from tessera_core.autonomy.stats import bootstrap_lower_bound
from tessera_core.policy.context import AutonomyLevel

__all__ = [
    "AUTONOMY_GATE_RULE_ID",
    "DEFAULT_CONFIG_PATH",
    "TRIGGER_COVERAGE_COLLAPSE",
    "TRIGGER_KILL_SWITCH",
    "TRIGGER_VIOLATION",
    "Assessment",
    "AutonomyConfig",
    "AutonomyConfigError",
    "AutonomyController",
    "AutonomyLevel",
    "AutonomyState",
    "CoverageObservation",
    "Criterion",
    "Evidence",
    "SavingObservation",
    "ShadowObservation",
    "TransitionKind",
    "ViolationObservation",
    "autonomy_config_from_text",
    "bootstrap_lower_bound",
    "in_window",
    "is_shadow_decision",
    "load_autonomy_config",
]

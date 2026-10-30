"""tessera_core.policy: the deterministic gate between a proposal and any execution.

Pure logic. The only file I/O is `load_ruleset` reading the YAML rule files. No network,
database or LLM imports, and no eval/exec.
"""

from tessera_core.policy import builtin_rules  # noqa: F401  (registers the built-in rule types)
from tessera_core.policy.context import AutonomyLevel, ForecastConfidence, PolicyContext
from tessera_core.policy.engine import POLICY_ERROR, evaluate
from tessera_core.policy.loader import DEFAULT_RULES_DIR, load_ruleset, ruleset_from_yaml_texts
from tessera_core.policy.registry import register_rule, registered_types
from tessera_core.policy.rule import Rule, RuleConfig, RuleOutcome, RuleResult
from tessera_core.policy.ruleset import Ruleset, RulesetError, RuleSpec, build_ruleset

__all__ = [
    "DEFAULT_RULES_DIR",
    "POLICY_ERROR",
    "AutonomyLevel",
    "ForecastConfidence",
    "PolicyContext",
    "Rule",
    "RuleConfig",
    "RuleOutcome",
    "RuleResult",
    "RuleSpec",
    "Ruleset",
    "RulesetError",
    "build_ruleset",
    "evaluate",
    "load_ruleset",
    "register_rule",
    "registered_types",
    "ruleset_from_yaml_texts",
]

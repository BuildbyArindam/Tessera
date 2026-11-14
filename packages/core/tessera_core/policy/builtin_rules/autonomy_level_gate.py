"""autonomy_level_gate: what the current autonomy level lets Tessera do WITHOUT a person (Day 14).

The level table is this rule's YAML `levels`, so it is part of the ruleset hash: a decision names the exact table it
was judged by. For the level in `context.autonomy_level`:

* an action type the level does not allow            -> BLOCK. A human approval cannot override it (the worker
  re-runs policy at execution time and a fresh BLOCK always stops). At L0 that is every action: advise-only.
* a money-moving action above the level's autonomous cap -> ESCALATE: a person decides.
* a money-moving action in a currency with no cap at the level -> ESCALATE (nothing autonomous without a limit).
* anything else                                       -> PASS.

The table is checked at load time: every level L0..L3 present, L0 allows nothing, and each level allows at least what
the one below it allows with caps at least as large. A table that quietly gave L1 more than L2 would not load.
"""

from __future__ import annotations

from typing import Any

from pydantic import field_validator, model_validator

from tessera_core.domain.actions import ActionType, Proposal
from tessera_core.domain.base import DomainModel
from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money
from tessera_core.policy.context import AutonomyLevel, PolicyContext, require
from tessera_core.policy.proposal_view import ProposalFormatError, proposed_amount
from tessera_core.policy.registry import register_rule
from tessera_core.policy.rule import Rule, RuleConfig, RuleOutcome, RuleResult

LEVEL_NAMES: tuple[str, ...] = tuple(f"L{level.value}" for level in AutonomyLevel)


class LevelSpec(DomainModel):
    # A sorted tuple, not a set: the ruleset hash serialises the config, and set order varies between processes.
    allowed_actions: tuple[ActionType, ...] = ()
    autonomous_cap: dict[Currency, Money] = {}

    @field_validator("allowed_actions", mode="after")
    @classmethod
    def _sorted_unique(cls, value: tuple[ActionType, ...]) -> tuple[ActionType, ...]:
        if len(set(value)) != len(value):
            raise ValueError("allowed_actions lists an action twice")
        return tuple(sorted(value))

    @field_validator("autonomous_cap", mode="before")
    @classmethod
    def _wrap_caps(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("autonomous_cap must map a currency code to an amount")
        # Through Money, so floats, NaN and absurd values are rejected exactly as proposal amounts are.
        return {key: {"amount": amount, "currency": key} for key, amount in value.items()}

    @field_validator("autonomous_cap", mode="after")
    @classmethod
    def _caps_positive(cls, value: dict[Currency, Money]) -> dict[Currency, Money]:
        for currency, cap in value.items():
            if not cap.is_positive():
                raise ValueError(f"cap for {currency.value} must be strictly positive")
        return value


class AutonomyGateConfig(RuleConfig):
    levels: dict[str, LevelSpec]

    @model_validator(mode="after")
    def _check_table(self) -> AutonomyGateConfig:
        if sorted(self.levels) != sorted(LEVEL_NAMES):
            raise ValueError(f"levels must be exactly {', '.join(LEVEL_NAMES)}")
        if self.levels["L0"].allowed_actions:
            raise ValueError("L0 is advise-only: it must allow no actions")
        for lower, upper in zip(LEVEL_NAMES, LEVEL_NAMES[1:], strict=False):
            low, up = self.levels[lower], self.levels[upper]
            if not set(low.allowed_actions) <= set(up.allowed_actions):
                raise ValueError(f"{upper} must allow every action {lower} allows")
            for currency, cap in low.autonomous_cap.items():
                higher = up.autonomous_cap.get(currency)
                if higher is None or higher < cap:
                    raise ValueError(f"{upper} cap for {currency.value} must be at least the {lower} cap")
        return self


@register_rule
class AutonomyLevelGate(Rule[AutonomyGateConfig]):
    type_name = "autonomy_level_gate"
    config_model = AutonomyGateConfig
    required_context = ("autonomy_level",)

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        level = require(context.autonomy_level, "autonomy_level")
        name = f"L{level.value}"
        spec = self.config.levels[name]
        if proposal.action_type not in spec.allowed_actions:
            return self.result(
                RuleOutcome.BLOCK, f"{proposal.action_type.value} is not permitted at autonomy level {name}"
            )
        try:
            amount = proposed_amount(proposal)
        except ProposalFormatError as exc:
            return self.result(RuleOutcome.BLOCK, f"invalid proposal amount: {exc}")
        if amount is None:
            return self.result(RuleOutcome.PASS)
        cap = spec.autonomous_cap.get(amount.currency)
        if cap is None:
            return self.result(
                RuleOutcome.ESCALATE, f"no autonomous cap for {amount.currency.value} at {name}; a person must approve"
            )
        if amount > cap:
            return self.result(
                RuleOutcome.ESCALATE,
                f"amount {amount} exceeds the {name} autonomous cap of {cap}; a person must approve",
            )
        return self.result(RuleOutcome.PASS)

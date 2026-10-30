"""max_amount_per_action: a hard ceiling on the money a single action may move, per currency.

An amount equal to the cap is allowed; one cent over is blocked. A currency with no configured
cap is blocked (fail closed) rather than assumed unlimited. Actions that move no money (HOLD,
FLAG_INVOICE) pass.
"""

from pydantic import field_validator, model_validator

from tessera_core.domain.actions import Proposal
from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money
from tessera_core.policy.context import PolicyContext
from tessera_core.policy.proposal_view import ProposalFormatError, proposed_amount
from tessera_core.policy.registry import register_rule
from tessera_core.policy.rule import Rule, RuleConfig, RuleOutcome, RuleResult


class MaxAmountConfig(RuleConfig):
    max_amount: dict[Currency, Money]

    @field_validator("max_amount", mode="before")
    @classmethod
    def _wrap_amounts(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("max_amount must map a currency code to an amount")
        # Routing every cap through Money rejects floats, NaN and absurd values the same way
        # proposal amounts are rejected.
        return {key: {"amount": amount, "currency": key} for key, amount in value.items()}

    @model_validator(mode="after")
    def _check(self) -> "MaxAmountConfig":
        if not self.max_amount:
            raise ValueError("max_amount must define at least one currency")
        for currency, cap in self.max_amount.items():
            if not cap.is_positive():
                raise ValueError(f"cap for {currency.value} must be strictly positive")
        return self


@register_rule
class MaxAmountPerAction(Rule[MaxAmountConfig]):
    type_name = "max_amount_per_action"
    config_model = MaxAmountConfig

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        try:
            amount = proposed_amount(proposal)
        except ProposalFormatError as exc:
            return self.result(RuleOutcome.BLOCK, f"invalid proposal amount: {exc}")
        if amount is None:
            return self.result(RuleOutcome.PASS)
        cap = self.config.max_amount.get(amount.currency)
        if cap is None:
            return self.result(RuleOutcome.BLOCK, f"no per-action cap is configured for {amount.currency.value}")
        if amount > cap:
            return self.result(RuleOutcome.BLOCK, f"amount {amount} exceeds the per-action cap of {cap}")
        return self.result(RuleOutcome.PASS)

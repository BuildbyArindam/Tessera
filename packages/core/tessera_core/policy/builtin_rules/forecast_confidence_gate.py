"""forecast_confidence_gate: wide forecast uncertainty sends a money movement to a human.

`relative_width` is the forecast interval width divided by the point estimate for the currency
leaving the account. Above the threshold -> ESCALATE; equal passes. Missing confidence data is also
ESCALATE (not BLOCK): the action may be fine, but nobody can say how sure the forecast is, so a
human decides. This rule therefore declares no required context and handles absence itself.

An invalid amount is still BLOCK, as in every money rule. The caller omits a currency whose point
estimate is zero (relative width undefined); that too is ESCALATE.
"""

from tessera_core.domain.actions import Proposal
from tessera_core.policy.context import PolicyContext
from tessera_core.policy.numeric import Ratio
from tessera_core.policy.proposal_view import ProposalFormatError, proposed_amount
from tessera_core.policy.registry import register_rule
from tessera_core.policy.rule import Rule, RuleConfig, RuleOutcome, RuleResult


class ConfidenceGateConfig(RuleConfig):
    max_relative_width: Ratio


@register_rule
class ForecastConfidenceGate(Rule[ConfidenceGateConfig]):
    type_name = "forecast_confidence_gate"
    config_model = ConfidenceGateConfig

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        try:
            amount = proposed_amount(proposal)
        except ProposalFormatError as exc:
            return self.result(RuleOutcome.BLOCK, f"invalid proposal amount: {exc}")
        if amount is None:
            return self.result(RuleOutcome.PASS)

        confidence = None if context.forecast_confidence is None else context.forecast_confidence.get(amount.currency)
        if confidence is None:
            return self.result(RuleOutcome.ESCALATE, f"no forecast confidence data for {amount.currency.value}")
        limit = self.config.max_relative_width
        if confidence.relative_width > limit:
            return self.result(
                RuleOutcome.ESCALATE,
                f"{amount.currency.value} forecast interval is {confidence.relative_width} of the point estimate; "
                f"the limit is {limit}",
            )
        return self.result(RuleOutcome.PASS)

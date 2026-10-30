"""daily_cumulative_cap: a ceiling on the total approved value per day, per currency.

`max_amount_per_action` limits one action; this limits the sum, so splitting a large payment into
many capped pieces still runs into a wall. The caller supplies `spend_today` (everything already
approved today, including approved-but-not-yet-executed actions) and decides where "today" starts.
A currency absent from `spend_today` has had nothing approved today. A currency with no cap
configured is blocked, never assumed unlimited. HOLD and FLAG_INVOICE move no money and pass.
"""

from tessera_core.domain.actions import Proposal
from tessera_core.domain.money import Money
from tessera_core.policy.context import PolicyContext, require
from tessera_core.policy.numeric import CurrencyLimits
from tessera_core.policy.proposal_view import (
    MONEY_MOVING_ACTIONS,
    ProposalFormatError,
    proposed_amount,
)
from tessera_core.policy.registry import register_rule
from tessera_core.policy.rule import Rule, RuleConfig, RuleOutcome, RuleResult


class DailyCapConfig(RuleConfig):
    daily_cap: CurrencyLimits


@register_rule
class DailyCumulativeCap(Rule[DailyCapConfig]):
    type_name = "daily_cumulative_cap"
    config_model = DailyCapConfig

    def required_context_for(self, proposal: Proposal) -> tuple[str, ...]:
        return ("spend_today",) if proposal.action_type in MONEY_MOVING_ACTIONS else ()

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        try:
            amount = proposed_amount(proposal)
        except ProposalFormatError as exc:
            return self.result(RuleOutcome.BLOCK, f"invalid proposal amount: {exc}")
        if amount is None:
            return self.result(RuleOutcome.PASS)
        cap = self.config.daily_cap.get(amount.currency)
        if cap is None:
            return self.result(RuleOutcome.BLOCK, f"no daily cap is configured for {amount.currency.value}")
        spent = require(context.spend_today, "spend_today").get(amount.currency, Money.zero(amount.currency))
        if spent + amount > cap:
            return self.result(
                RuleOutcome.BLOCK, f"{spent} already approved today plus {amount} exceeds the daily cap of {cap}"
            )
        return self.result(RuleOutcome.PASS)

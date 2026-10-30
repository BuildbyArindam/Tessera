"""currency_concentration_cap: one conversion may not spend more than X percent of the source balance.

Stops a single instruction from emptying a currency. The comparison is exact decimal arithmetic
(amount * 100 against balance * X), with no rounded intermediate, so equal to the limit passes and
one cent over blocks. A missing, zero or negative balance leaves nothing to convert, so any amount
is blocked. Only CONVERT is checked.
"""

from tessera_core.domain.actions import ActionType, Proposal
from tessera_core.domain.money import Money
from tessera_core.policy.context import PolicyContext, require
from tessera_core.policy.numeric import EXACT, Percent
from tessera_core.policy.proposal_view import ProposalFormatError, proposed_amount
from tessera_core.policy.registry import register_rule
from tessera_core.policy.rule import Rule, RuleConfig, RuleOutcome, RuleResult


class ConcentrationConfig(RuleConfig):
    max_percent_of_balance: Percent


@register_rule
class CurrencyConcentrationCap(Rule[ConcentrationConfig]):
    type_name = "currency_concentration_cap"
    config_model = ConcentrationConfig

    def required_context_for(self, proposal: Proposal) -> tuple[str, ...]:
        return ("balances",) if proposal.action_type is ActionType.CONVERT else ()

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        if proposal.action_type is not ActionType.CONVERT:
            return self.result(RuleOutcome.PASS)
        try:
            amount = proposed_amount(proposal)
        except ProposalFormatError as exc:
            return self.result(RuleOutcome.BLOCK, f"invalid proposal amount: {exc}")
        if amount is None:
            return self.result(RuleOutcome.PASS)

        balance = require(context.balances, "balances").get(amount.currency, Money.zero(amount.currency))
        percent = self.config.max_percent_of_balance
        if EXACT.multiply(amount.amount, 100) > EXACT.multiply(balance.amount, percent):
            return self.result(
                RuleOutcome.BLOCK, f"converting {amount} would exceed {percent}% of the {balance} balance"
            )
        return self.result(RuleOutcome.PASS)

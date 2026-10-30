"""quote_slippage_guard: block a conversion whose executable quote strays from the reference rate.

The reference rate and the executable quote both come from the context, never from the proposal:
the model must not be able to name the rate it is judged against. A quote that differs in either
direction by more than Y percent is BLOCK; a quote that is much *better* than the reference is as
suspicious as a much worse one (stale or wrong data). Equal to the limit passes.

Rates use the "SELL/BUY" convention (buy units per 1 sell unit). A missing reference or quote for
the pair is BLOCK, so a conversion cannot run unchecked because nobody fetched a quote.
The caller must therefore obtain a quote before asking for a decision on a CONVERT.
"""

from tessera_core.domain.actions import ActionType, Proposal
from tessera_core.policy.context import PolicyContext, require
from tessera_core.policy.numeric import EXACT, Percent
from tessera_core.policy.proposal_view import ProposalFormatError, buy_currency, proposed_amount
from tessera_core.policy.registry import register_rule
from tessera_core.policy.rule import Rule, RuleConfig, RuleOutcome, RuleResult


class SlippageConfig(RuleConfig):
    max_slippage_percent: Percent


@register_rule
class QuoteSlippageGuard(Rule[SlippageConfig]):
    type_name = "quote_slippage_guard"
    config_model = SlippageConfig

    def required_context_for(self, proposal: Proposal) -> tuple[str, ...]:
        if proposal.action_type is ActionType.CONVERT:
            return ("fx_reference_rate", "fx_quote_rate")
        return ()

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        if proposal.action_type is not ActionType.CONVERT:
            return self.result(RuleOutcome.PASS)
        try:
            amount = proposed_amount(proposal)
            buy = buy_currency(proposal)
        except ProposalFormatError as exc:
            return self.result(RuleOutcome.BLOCK, f"invalid conversion: {exc}")
        if amount is None or buy == amount.currency:
            return self.result(RuleOutcome.BLOCK, "invalid conversion: sell and buy currency must differ")

        pair = f"{amount.currency.value}/{buy.value}"
        reference = require(context.fx_reference_rate, "fx_reference_rate").get(pair)
        quote = require(context.fx_quote_rate, "fx_quote_rate").get(pair)
        if reference is None or quote is None:
            return self.result(RuleOutcome.BLOCK, f"no reference rate or executable quote for {pair}")

        percent = self.config.max_slippage_percent
        gap = EXACT.subtract(quote, reference).copy_abs()
        if EXACT.multiply(gap, 100) > EXACT.multiply(reference, percent):
            return self.result(
                RuleOutcome.BLOCK, f"{pair} quote {quote} is more than {percent}% away from reference {reference}"
            )
        return self.result(RuleOutcome.PASS)

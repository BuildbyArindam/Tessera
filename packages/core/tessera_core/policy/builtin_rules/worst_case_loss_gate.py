"""worst_case_loss_gate: a bad enough tail in the simulation sends the action to a human.

The simulator (Day 8) replays a proposal over bootstrapped FX paths and reports its 5th-percentile
loss against the comparator. If that loss is above the configured ceiling for the currency, the
action is ESCALATE: it may well be fine, but a person should accept the downside knowingly.
A loss equal to the ceiling passes. The ceiling is per currency (the SELL currency, the one the
business holds); a currency with no ceiling is BLOCK, never assumed unlimited.

When the simulation is required
- CONVERT and PAY_SUPPLIER: a summary is expected. Missing is ESCALATE (nobody can say how bad the
  downside is), the same stance as `forecast_confidence_gate`.
- HOLD: judged only if a summary is supplied (waiting can be the risky choice); none is fine.
- TOPUP_CARD and FLAG_INVOICE: not simulated, always PASS.

The summary lives in the CONTEXT, assembled by the caller from `simulate(...)`, never in the
proposal, so the model cannot state its own risk. It must belong to this proposal (same id, action
and amount); a summary for something else is BLOCK, so a stale or swapped report cannot vouch for
it, and neither can a small simulation for a larger action that reuses the id.
Reasons use only fixed text and validated amounts, never proposal free text.
"""

from tessera_core.domain.actions import ActionType, Proposal
from tessera_core.policy.context import PolicyContext
from tessera_core.policy.numeric import CurrencyLimits
from tessera_core.policy.proposal_view import ProposalFormatError, read_amount
from tessera_core.policy.registry import register_rule
from tessera_core.policy.rule import Rule, RuleConfig, RuleOutcome, RuleResult

_REQUIRES_SIMULATION = frozenset({ActionType.CONVERT, ActionType.PAY_SUPPLIER})
_JUDGED_WHEN_PRESENT = _REQUIRES_SIMULATION | {ActionType.HOLD}


class WorstCaseLossConfig(RuleConfig):
    max_worst_case_loss: CurrencyLimits


@register_rule
class WorstCaseLossGate(Rule[WorstCaseLossConfig]):
    type_name = "worst_case_loss_gate"
    config_model = WorstCaseLossConfig

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        action = proposal.action_type
        if action not in _JUDGED_WHEN_PRESENT:
            return self.result(RuleOutcome.PASS)

        summary = context.simulation
        if summary is None:
            if action in _REQUIRES_SIMULATION:
                return self.result(RuleOutcome.ESCALATE, "no simulation was supplied, so the downside is unknown")
            return self.result(RuleOutcome.PASS)

        try:
            amount = read_amount(proposal)
        except ProposalFormatError as exc:
            return self.result(RuleOutcome.BLOCK, f"invalid proposal amount: {exc}")
        if summary.proposal_id != proposal.id or summary.action_type is not action or summary.amount != amount:
            return self.result(RuleOutcome.BLOCK, "the supplied simulation belongs to a different proposal")

        loss = summary.worst_case_loss
        ceiling = self.config.max_worst_case_loss.get(loss.currency)
        if ceiling is None:
            return self.result(RuleOutcome.BLOCK, f"no worst-case loss ceiling is configured for {loss.currency.value}")
        if loss > ceiling:
            return self.result(
                RuleOutcome.ESCALATE,
                f"simulated 5th-percentile loss of {loss} exceeds the ceiling of {ceiling}",
            )
        return self.result(RuleOutcome.PASS)

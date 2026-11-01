"""early_payment_gate: paying a supplier well before the obligation is due goes to a person.

Found by the Day 10 red team (RT-03): a note told a fooled model to pay an APPROVED supplier 400,000 INR nine
days early. Every other rule passed it (approved payee, inside the caps), so with the detector bypassed it was
the one action that executed. The due date is the one thing in that attack no rule was checking.

The due date comes from `PolicyContext.obligation_due_dates`, which the caller builds from its own obligation
records; the proposal only names the obligation id, and the model cannot supply a date. The rule only
escalates (an early payment is sometimes right, for a discount or a risk reason), and it fails closed: a
PAY_SUPPLIER that names no obligation, an obligation the caller does not know, or no due dates at all is ESCALATE.
A payment that is late is not this rule's concern. Other actions are ignored.

This narrows the gap, it does not close it: it covers WHEN a supplier is paid, not other in-policy choices a
fooled model could make, such as how much to convert now.
"""

from pydantic import Field

from tessera_core.domain.actions import ActionType, Proposal
from tessera_core.policy.context import PolicyContext, require
from tessera_core.policy.proposal_view import ProposalFormatError, proposal_id
from tessera_core.policy.registry import register_rule
from tessera_core.policy.rule import Rule, RuleConfig, RuleOutcome, RuleResult


class EarlyPaymentConfig(RuleConfig):
    max_days_early: int = Field(strict=True, ge=0, le=365)


@register_rule
class EarlyPaymentGate(Rule[EarlyPaymentConfig]):
    type_name = "early_payment_gate"
    config_model = EarlyPaymentConfig

    def required_context_for(self, proposal: Proposal) -> tuple[str, ...]:
        return ("now",) if proposal.action_type is ActionType.PAY_SUPPLIER else ()

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        if proposal.action_type is not ActionType.PAY_SUPPLIER:
            return self.result(RuleOutcome.PASS)
        try:
            obligation = proposal_id(proposal, "obligation_id")
        except ProposalFormatError:
            return self.result(RuleOutcome.ESCALATE, "the payment names no obligation, so its due date is unknown")
        due = (context.obligation_due_dates or {}).get(obligation)
        if due is None:
            return self.result(RuleOutcome.ESCALATE, "the due date of the obligation being paid is not known")
        early = (due - require(context.now, "now").date()).days
        limit = self.config.max_days_early
        if early > limit:
            return self.result(RuleOutcome.ESCALATE, f"payment is {early} days before it is due; the limit is {limit}")
        return self.result(RuleOutcome.PASS)

"""untrusted_input_gate: text a third party wrote that looked like an attack sends the run to a human.

The planner scans every invoice note, memo, name and email body it is shown (and the model's own
rationale) for instruction-like phrases, fake system markup, hidden or look-alike characters, encodings
and payment destinations. If anything is found, the codes arrive here in `PolicyContext.input_screening`
and EVERY proposal from that run is ESCALATE: a person decides, whatever the model proposed.

This rule only ever escalates. It cannot approve anything and it cannot weaken another rule: the engine
combines results so any BLOCK still wins, which is why a flagged attack on the allowlist or a cap is
BLOCK, not merely ESCALATE. The detector behind it is a signal that can be evaded; the other rules do
not depend on it.

A context with no screening result at all is also ESCALATE for an action that can move money (the same
stance as `forecast_confidence_gate` and `worst_case_loss_gate`): nobody can say the text behind it was
clean. A HOLD or FLAG_INVOICE moves nothing, so a missing result alone does not stop it, which keeps the
Day 7 principle that such actions need only the kill-switch context. A result that carries flags
escalates every action, because that is an explicit signal. The rule declares no required context and
handles absence itself.

Reasons use only fixed text and the flag codes, which the context type restricts to short lowercase
identifiers, never any text the third party wrote.
"""

from tessera_core.domain.actions import Proposal
from tessera_core.policy.context import PolicyContext
from tessera_core.policy.proposal_view import MONEY_MOVING_ACTIONS
from tessera_core.policy.registry import register_rule
from tessera_core.policy.rule import Rule, RuleConfig, RuleOutcome, RuleResult

_NOT_SCREENED = "no input screening result was supplied, so the text behind this proposal is unchecked"


class UntrustedInputGateConfig(RuleConfig):
    """No parameters: any flag escalates. A threshold here would be a dial an attacker could aim under."""


@register_rule
class UntrustedInputGate(Rule[UntrustedInputGateConfig]):
    type_name = "untrusted_input_gate"
    config_model = UntrustedInputGateConfig

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        screening = context.input_screening
        if screening is None:
            if proposal.action_type in MONEY_MOVING_ACTIONS:
                return self.result(RuleOutcome.ESCALATE, _NOT_SCREENED)
            return self.result(RuleOutcome.PASS)
        if screening.flag_codes:
            codes = ", ".join(screening.flag_codes)
            return self.result(RuleOutcome.ESCALATE, f"untrusted text behind this proposal was flagged ({codes})")
        return self.result(RuleOutcome.PASS)

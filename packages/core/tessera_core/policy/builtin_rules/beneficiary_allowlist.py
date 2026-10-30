"""beneficiary_allowlist: supplier payouts go only to approved beneficiaries.

- Not on the allowlist (or no valid `beneficiary_id`)  -> BLOCK.
- On the allowlist but added less than the cooldown ago -> ESCALATE: a human confirms a payee
  that appeared in the last day. A beneficiary whose added time is unknown, or lies in the
  future, is treated the same way.
- Otherwise PASS.

Proposals name a beneficiary by id only. The id is validated to a strict shape before it is looked
up or put in a reason, so injected text cannot ride along. Only PAY_SUPPLIER is a payout.
"""

from datetime import timedelta

from pydantic import Field

from tessera_core.domain.actions import ActionType, Proposal
from tessera_core.policy.context import PolicyContext, require
from tessera_core.policy.proposal_view import ProposalFormatError, proposal_id
from tessera_core.policy.registry import register_rule
from tessera_core.policy.rule import Rule, RuleConfig, RuleOutcome, RuleResult


class BeneficiaryConfig(RuleConfig):
    new_beneficiary_cooldown_hours: int = Field(strict=True, ge=1, le=720)


@register_rule
class BeneficiaryAllowlist(Rule[BeneficiaryConfig]):
    type_name = "beneficiary_allowlist"
    config_model = BeneficiaryConfig

    def required_context_for(self, proposal: Proposal) -> tuple[str, ...]:
        if proposal.action_type is ActionType.PAY_SUPPLIER:
            return ("beneficiary_allowlist", "beneficiary_added_at", "now")
        return ()

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        if proposal.action_type is not ActionType.PAY_SUPPLIER:
            return self.result(RuleOutcome.PASS)
        try:
            beneficiary = proposal_id(proposal, "beneficiary_id")
        except ProposalFormatError as exc:
            return self.result(RuleOutcome.BLOCK, f"invalid payout: {exc}")
        if beneficiary not in require(context.beneficiary_allowlist, "beneficiary_allowlist"):
            return self.result(RuleOutcome.BLOCK, f"beneficiary {beneficiary} is not on the approved list")

        hours = self.config.new_beneficiary_cooldown_hours
        added_at = require(context.beneficiary_added_at, "beneficiary_added_at").get(beneficiary)
        if added_at is None:
            return self.result(RuleOutcome.ESCALATE, f"beneficiary {beneficiary} has no recorded added time")
        if require(context.now, "now") - added_at < timedelta(hours=hours):
            return self.result(RuleOutcome.ESCALATE, f"beneficiary {beneficiary} was added within the last {hours}h")
        return self.result(RuleOutcome.PASS)

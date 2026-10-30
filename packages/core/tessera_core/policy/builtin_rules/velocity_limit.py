"""velocity_limit: no more than N approved money-moving actions in any rolling hour.

Counts the approvals in `recent_approval_times` that fall inside the hour before `now`; once the
count reaches N the next money movement is BLOCK. An approval timestamped in the future is
counted (the conservative reading of a clock fault). HOLD and FLAG_INVOICE move no money and pass.
This is the second wall, beside daily_cumulative_cap, against splitting one big action into many.
"""

from datetime import timedelta

from pydantic import Field

from tessera_core.domain.actions import Proposal
from tessera_core.policy.context import PolicyContext, require
from tessera_core.policy.proposal_view import MONEY_MOVING_ACTIONS
from tessera_core.policy.registry import register_rule
from tessera_core.policy.rule import Rule, RuleConfig, RuleOutcome, RuleResult

WINDOW = timedelta(hours=1)


class VelocityConfig(RuleConfig):
    max_actions_per_hour: int = Field(strict=True, ge=1, le=10_000)


@register_rule
class VelocityLimit(Rule[VelocityConfig]):
    type_name = "velocity_limit"
    config_model = VelocityConfig

    def required_context_for(self, proposal: Proposal) -> tuple[str, ...]:
        return ("recent_approval_times", "now") if proposal.action_type in MONEY_MOVING_ACTIONS else ()

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        if proposal.action_type not in MONEY_MOVING_ACTIONS:
            return self.result(RuleOutcome.PASS)
        cutoff = require(context.now, "now") - WINDOW
        recent = sum(1 for t in require(context.recent_approval_times, "recent_approval_times") if t > cutoff)
        limit = self.config.max_actions_per_hour
        if recent >= limit:
            return self.result(RuleOutcome.BLOCK, f"{recent} actions approved in the last hour; the limit is {limit}")
        return self.result(RuleOutcome.PASS)

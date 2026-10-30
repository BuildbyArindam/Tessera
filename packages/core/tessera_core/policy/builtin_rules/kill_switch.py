"""kill_switch: when the operator has engaged it, every proposal is blocked, whatever it is."""

from tessera_core.domain.actions import Proposal
from tessera_core.policy.context import PolicyContext
from tessera_core.policy.registry import register_rule
from tessera_core.policy.rule import Rule, RuleConfig, RuleOutcome, RuleResult


class KillSwitchConfig(RuleConfig):
    """No parameters. The switch's state is runtime context, not configuration."""


@register_rule
class KillSwitch(Rule[KillSwitchConfig]):
    type_name = "kill_switch"
    config_model = KillSwitchConfig
    required_context = ("kill_switch_engaged",)

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        if context.kill_switch_engaged:
            return self.result(RuleOutcome.BLOCK, "kill switch is engaged; no actions are allowed")
        return self.result(RuleOutcome.PASS)

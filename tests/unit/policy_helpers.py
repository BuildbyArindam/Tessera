"""Shared builders and test-only rule types for the policy tests."""

from datetime import UTC, datetime
from typing import Any

from tessera_core.domain import ActionType, Proposal
from tessera_core.policy import (
    PolicyContext,
    Rule,
    RuleConfig,
    RuleOutcome,
    RuleResult,
    Ruleset,
    RuleSpec,
    build_ruleset,
    register_rule,
)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def make_proposal(
    action: ActionType = ActionType.CONVERT,
    amount: object = "100.00",
    currency: object = "USD",
    rationale: str = "because",
    **extra: Any,
) -> Proposal:
    params: dict[str, Any] = {"amount": amount, "currency": currency, **extra}
    return Proposal(id="p1", action_type=action, params=params, rationale=rationale, confidence=0.5, created_at=NOW)


def make_context(kill: bool | None = False, **kwargs: Any) -> PolicyContext:
    return PolicyContext(kill_switch_engaged=kill, **kwargs)


# ---- test-only rule types (registered once at import) -------------------------------------
class _Empty(RuleConfig):
    pass


@register_rule
class AlwaysPass(Rule[_Empty]):
    type_name = "test_pass"
    config_model = _Empty

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        return self.result(RuleOutcome.PASS)


@register_rule
class AlwaysEscalate(Rule[_Empty]):
    type_name = "test_escalate"
    config_model = _Empty

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        return self.result(RuleOutcome.ESCALATE, "needs a human")


@register_rule
class AlwaysBlock(Rule[_Empty]):
    type_name = "test_block"
    config_model = _Empty

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        return self.result(RuleOutcome.BLOCK, "nope")


@register_rule
class Boom(Rule[_Empty]):
    type_name = "test_boom"
    config_model = _Empty

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        raise RuntimeError("secret internal detail")


@register_rule
class WrongId(Rule[_Empty]):
    type_name = "test_wrong_id"
    config_model = _Empty

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        return RuleResult(rule_id="someone_else", outcome=RuleOutcome.PASS)


@register_rule
class NeedsBalances(Rule[_Empty]):
    type_name = "test_needs_balances"
    config_model = _Empty
    required_context = ("balances",)

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        return self.result(RuleOutcome.PASS)


def ruleset_with(*extra_types: str) -> Ruleset:
    """kill_switch plus one instance of each named test rule type (id == type)."""
    specs = [RuleSpec(id="kill_switch", type="kill_switch")]
    specs += [RuleSpec(id=t, type=t) for t in extra_types]
    return build_ruleset(specs)

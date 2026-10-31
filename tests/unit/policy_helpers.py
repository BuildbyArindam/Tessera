"""Shared builders and test-only rule types for the policy tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from tessera_core.domain import ActionType, Money, Proposal, SimulationSummary
from tessera_core.domain.currency import Currency
from tessera_core.policy import (
    DEFAULT_RULES_DIR,
    ForecastConfidence,
    PolicyContext,
    Rule,
    RuleConfig,
    RuleOutcome,
    RuleResult,
    Ruleset,
    RuleSpec,
    build_ruleset,
    register_rule,
    ruleset_from_yaml_texts,
)
from tessera_core.policy.proposal_view import ProposalFormatError, read_amount

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


def day6_ruleset() -> Ruleset:
    """The Day 6 shipped rules only (kill switch + per-action cap), for the Day 6 engine tests."""
    names = ("kill_switch.yaml", "max_amount_per_action.yaml")
    return ruleset_from_yaml_texts({n: (DEFAULT_RULES_DIR / n).read_text(encoding="utf-8") for n in names})


def usd(amount: str) -> Money:
    return Money(amount=amount, currency=Currency.USD)


def sim_for(
    proposal: Proposal, loss: str = "0.00", saving: str = "0.00", currency: str | None = None
) -> SimulationSummary:
    """A simulation summary that belongs to `proposal` (same id, action and amount), in its sell currency.

    The sell currency defaults to the proposal's own `currency` param when that is a real code, else
    USD; tests that need a different one (a PAY_SUPPLIER funded from elsewhere) pass `currency`.
    """
    raw = currency if currency is not None else proposal.params.get("currency")
    code = Currency(raw) if isinstance(raw, str) and raw in {c.value for c in Currency} else Currency.USD
    try:
        amount = read_amount(proposal)
    except ProposalFormatError:  # a malformed proposal: any placeholder will do, the money rules BLOCK it anyway
        amount = Money(amount="1.00", currency=code)
    return SimulationSummary(
        proposal_id=proposal.id,
        action_type=proposal.action_type,
        amount=amount,
        worst_case_loss=Money(amount=loss, currency=code),
        expected_saving=Money(amount=saving, currency=code),
        n_paths=5000,
    )


def full_context(**overrides: Any) -> PolicyContext:
    """A context with every Day 7 field supplied, in which a small ordinary action is clean.

    Pass `field=None` to remove a field, or another value to replace it.
    """
    values: dict[str, Any] = {
        "kill_switch_engaged": False,
        "now": NOW,
        "balances": {Currency.USD: usd("10000.00"), Currency.EUR: Money(amount="8000.00", currency=Currency.EUR)},
        "spend_today": {},
        "beneficiary_allowlist": frozenset({"sup_old", "sup_new"}),
        "beneficiary_added_at": {"sup_old": NOW - timedelta(days=30), "sup_new": NOW - timedelta(hours=2)},
        "recent_approval_times": (),
        "fx_reference_rate": {"USD/EUR": Decimal("0.9200")},
        "fx_quote_rate": {"USD/EUR": Decimal("0.9200")},
        "forecast_confidence": {
            Currency.USD: ForecastConfidence(relative_width=Decimal("0.2")),
            Currency.EUR: ForecastConfidence(relative_width=Decimal("0.2")),
        },
        "card_loaded_this_month": {},
    }
    values.update(overrides)
    return PolicyContext(**values)


def clean_proposals() -> dict[str, Proposal]:
    """One small proposal per money-moving action that every shipped rule passes."""
    return {
        "convert": make_proposal(ActionType.CONVERT, "1000.00", "USD", to_currency="EUR"),
        "pay": make_proposal(ActionType.PAY_SUPPLIER, "500.00", "USD", beneficiary_id="sup_old"),
        "card": make_proposal(ActionType.TOPUP_CARD, "100.00", "USD", vendor_id="workspace_suite"),
    }


def only(rule_file: str, yaml_text: str | None = None) -> Ruleset:
    """kill_switch plus one shipped rule file (or the same file with replacement text)."""
    text = yaml_text if yaml_text is not None else (DEFAULT_RULES_DIR / rule_file).read_text(encoding="utf-8")
    kill = (DEFAULT_RULES_DIR / "kill_switch.yaml").read_text(encoding="utf-8")
    return ruleset_from_yaml_texts({"kill_switch.yaml": kill, rule_file: text})

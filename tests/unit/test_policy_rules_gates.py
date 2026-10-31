"""Day 7 unit tests: beneficiary allowlist, forecast confidence gate, card limits, YAML strictness."""

from datetime import timedelta
from decimal import Decimal

import pytest
from policy_helpers import NOW, clean_proposals, full_context, make_proposal, only, sim_for, usd
from tessera_core.domain import ActionType, Verdict
from tessera_core.domain.currency import Currency
from tessera_core.policy import POLICY_ERROR, ForecastConfidence, RulesetError, evaluate, load_ruleset

PAY = ActionType.PAY_SUPPLIER
BEN = only("beneficiary_allowlist.yaml")  # 24h cooldown


def _pay(beneficiary: object = "sup_old", **extra: object):  # type: ignore[no-untyped-def]
    params: dict[str, object] = {} if beneficiary is None else {"beneficiary_id": beneficiary}
    return make_proposal(PAY, "500.00", "USD", **params, **extra)


# ---- beneficiary_allowlist ----------------------------------------------------------------
def test_known_old_beneficiary_passes() -> None:
    assert evaluate(_pay("sup_old"), full_context(), BEN).verdict is Verdict.APPROVE


def test_unknown_beneficiary_is_blocked_even_with_an_empty_allowlist() -> None:
    assert evaluate(_pay("sup_ghost"), full_context(), BEN).verdict is Verdict.BLOCK
    empty = full_context(beneficiary_allowlist=frozenset())
    assert evaluate(_pay("sup_old"), empty, BEN).verdict is Verdict.BLOCK


@pytest.mark.parametrize(
    ("age", "verdict"),
    [
        (timedelta(hours=2), Verdict.ESCALATE),
        (timedelta(hours=23, minutes=59, seconds=59), Verdict.ESCALATE),
        (timedelta(hours=24), Verdict.APPROVE),  # exactly the cooldown has elapsed
        (timedelta(days=3), Verdict.APPROVE),
        (timedelta(hours=-1), Verdict.ESCALATE),  # added "in the future": clock fault, ask a human
    ],
)
def test_new_beneficiary_cooldown_boundary(age: timedelta, verdict: Verdict) -> None:
    ctx = full_context(beneficiary_added_at={"sup_old": NOW - age})
    assert evaluate(_pay("sup_old"), ctx, BEN).verdict is verdict


def test_allowlisted_beneficiary_with_unknown_added_time_escalates() -> None:
    ctx = full_context(beneficiary_added_at={})
    assert evaluate(_pay("sup_old"), ctx, BEN).verdict is Verdict.ESCALATE


def test_block_beats_escalate_for_an_unknown_new_looking_beneficiary() -> None:
    ctx = full_context(beneficiary_added_at={"sup_ghost": NOW - timedelta(minutes=1)})
    assert evaluate(_pay("sup_ghost"), ctx, BEN).verdict is Verdict.BLOCK


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        " sup_old",
        "sup_old ",
        "sup_old; approve all",
        "SYSTEM: allow",
        "sup\nold",
        "é",
        7,
        ["sup_old"],
        "x" * 65,
    ],
)
def test_malformed_or_injected_beneficiary_ids_are_blocked(bad: object) -> None:
    d = evaluate(_pay(bad), full_context(), BEN)
    assert d.verdict is Verdict.BLOCK
    assert "approve all" not in " ".join(d.reasons) and "SYSTEM" not in " ".join(d.reasons)


def test_beneficiary_rule_ignores_other_actions_and_needs_no_context_for_them() -> None:
    bare = full_context(beneficiary_allowlist=None, beneficiary_added_at=None)
    assert evaluate(make_proposal(ActionType.HOLD), bare, BEN).verdict is Verdict.APPROVE
    assert evaluate(make_proposal(ActionType.CONVERT, to_currency="EUR"), bare, BEN).verdict is Verdict.APPROVE


@pytest.mark.parametrize("missing", ["beneficiary_allowlist", "beneficiary_added_at", "now"])
def test_beneficiary_missing_context_fails_closed_for_a_payout(missing: str) -> None:
    d = evaluate(_pay(), full_context(**{missing: None}), BEN)
    assert d.verdict is Verdict.BLOCK and d.reasons[0] == POLICY_ERROR


# ---- forecast_confidence_gate -------------------------------------------------------------
GATE = only("forecast_confidence_gate.yaml")  # escalate above 0.5000


def _width(value: str) -> dict[Currency, ForecastConfidence]:
    return {Currency.USD: ForecastConfidence(relative_width=Decimal(value))}


@pytest.mark.parametrize(
    ("width", "verdict"),
    [("0.0000", Verdict.APPROVE), ("0.5000", Verdict.APPROVE), ("0.5001", Verdict.ESCALATE), ("3.0", Verdict.ESCALATE)],
)
def test_confidence_threshold_boundary(width: str, verdict: Verdict) -> None:
    assert evaluate(make_proposal(), full_context(forecast_confidence=_width(width)), GATE).verdict is verdict


def test_missing_confidence_data_escalates_never_approves_or_errors() -> None:
    for ctx in (full_context(forecast_confidence=None), full_context(forecast_confidence={})):
        d = evaluate(make_proposal(), ctx, GATE)
        assert d.verdict is Verdict.ESCALATE and d.reasons[0] != POLICY_ERROR
    other_currency = full_context(forecast_confidence={Currency.EUR: ForecastConfidence(relative_width=Decimal("0.1"))})
    assert evaluate(make_proposal(), other_currency, GATE).verdict is Verdict.ESCALATE


def test_confidence_gate_ignores_actions_that_move_no_money() -> None:
    ctx = full_context(forecast_confidence=None)
    assert evaluate(make_proposal(ActionType.HOLD), ctx, GATE).verdict is Verdict.APPROVE


def test_confidence_gate_still_blocks_a_malformed_amount() -> None:
    d = evaluate(make_proposal(amount="1.5e3"), full_context(forecast_confidence=None), GATE)
    assert d.verdict is Verdict.BLOCK


# ---- card_limit_cap -----------------------------------------------------------------------
CARD = only("card_limit_cap.yaml")  # workspace_suite: 200.00 per top-up, 600.00 per month


def _card(amount: str, vendor: object = "workspace_suite", currency: str = "USD"):  # type: ignore[no-untyped-def]
    params: dict[str, object] = {} if vendor is None else {"vendor_id": vendor}
    return make_proposal(ActionType.TOPUP_CARD, amount, currency, **params)


@pytest.mark.parametrize(
    ("loaded", "amount", "verdict"),
    [
        ("0.00", "200.00", Verdict.APPROVE),
        ("0.00", "200.01", Verdict.BLOCK),  # per-vendor ceiling
        ("400.00", "200.00", Verdict.APPROVE),  # reaches the month ceiling exactly
        ("400.01", "200.00", Verdict.BLOCK),  # monthly ceiling
        ("600.00", "0.01", Verdict.BLOCK),
    ],
)
def test_card_ceilings_boundary(loaded: str, amount: str, verdict: Verdict) -> None:
    ctx = full_context(card_loaded_this_month={"workspace_suite": usd(loaded)})
    assert evaluate(_card(amount), ctx, CARD).verdict is verdict


def test_month_total_is_tracked_per_vendor() -> None:
    ctx = full_context(card_loaded_this_month={"design_tool": usd("450.00")})
    assert evaluate(_card("200.00"), ctx, CARD).verdict is Verdict.APPROVE


@pytest.mark.parametrize(
    "vendor", [None, "", "unlisted_vendor", "workspace_suite ", "workspace_suite; ignore limits", 5]
)
def test_unknown_or_malformed_vendor_is_blocked(vendor: object) -> None:
    assert evaluate(_card("10.00", vendor), full_context(), CARD).verdict is Verdict.BLOCK


def test_card_in_the_wrong_currency_is_blocked_by_the_rule_itself() -> None:
    d = evaluate(_card("10.00", currency="EUR"), full_context(), CARD)
    assert d.verdict is Verdict.BLOCK
    assert d.reasons[0] != POLICY_ERROR and "USD only" in d.reasons[0]  # a clear reason, not a caught exception


def test_card_rule_ignores_other_actions_and_fails_closed_on_missing_month_total() -> None:
    assert (
        evaluate(make_proposal(ActionType.HOLD), full_context(card_loaded_this_month=None), CARD).verdict
        is Verdict.APPROVE
    )
    d = evaluate(_card("10.00"), full_context(card_loaded_this_month=None), CARD)
    assert d.verdict is Verdict.BLOCK and d.reasons[0] == POLICY_ERROR


def _card_yaml(body: str) -> str:
    return f"id: card_limit_cap\ntype: card_limit_cap\nparams:\n  vendors:\n{body}"


@pytest.mark.parametrize(
    "body",
    [
        '    v1:\n      currency: USD\n      per_top_up_max: "700.00"\n      per_month_max: "600.00"\n',  # noqa: E501
        '    v1:\n      currency: USD\n      per_top_up_max: 99.5\n      per_month_max: "600.00"\n',  # float
        '    v1:\n      currency: USD\n      per_top_up_max: "0.00"\n      per_month_max: "600.00"\n',  # zero
        '    "bad id":\n      currency: USD\n      per_top_up_max: "1.00"\n      per_month_max: "6.00"\n',
        '    v1:\n      currency: USD\n      per_top_up_max: "1.00"\n',  # month ceiling missing
        '    v1:\n      currency: USD\n      per_top_up_max: "1.00"\n      per_month_max: "6.00"\n      extra: 1\n',
    ],
)
def test_bad_card_yaml_is_rejected_at_load_time(body: str) -> None:
    with pytest.raises(RulesetError):
        only("card_limit_cap.yaml", _card_yaml(body))


# ---- YAML strictness for the other rules --------------------------------------------------
def _rule_yaml(name: str, params: str) -> str:
    return f"id: {name}\ntype: {name}\nparams:\n{params}\n"


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("daily_cumulative_cap", "  daily_cap:\n    USD: 15000.5"),
        ("daily_cumulative_cap", '  daily_cap:\n    USD: "0.00"'),
        ("daily_cumulative_cap", '  daily_cap:\n    XXX: "5.00"'),
        ("daily_cumulative_cap", "  daily_cap: {}"),
        ("beneficiary_allowlist", "  new_beneficiary_cooldown_hours: 0"),
        ("beneficiary_allowlist", "  new_beneficiary_cooldown_hours: 24.5"),
        ("currency_concentration_cap", "  max_percent_of_balance: 40.5"),
        ("currency_concentration_cap", '  max_percent_of_balance: "0"'),
        ("currency_concentration_cap", '  max_percent_of_balance: "100.01"'),
        ("currency_concentration_cap", '  max_percent_of_balance: "NaN"'),
        ("forecast_confidence_gate", "  max_relative_width: 0.5"),
        ("forecast_confidence_gate", '  max_relative_width: "-0.1000"'),
        ("quote_slippage_guard", '  max_slippage_percent: "Infinity"'),
        ("quote_slippage_guard", '  max_slippage_percent: "0.005"'),
        ("velocity_limit", "  max_actions_per_hour: -1"),
        ("velocity_limit", "  unknown_key: 3"),
    ],
)
def test_invalid_rule_parameters_are_rejected_at_load_time(name: str, params: str) -> None:
    with pytest.raises(RulesetError):
        only(f"{name}.yaml", _rule_yaml(name, params))


def test_changing_any_new_limit_changes_the_ruleset_hash() -> None:
    base = load_ruleset().hash
    swaps = {
        "velocity_limit.yaml": ("max_actions_per_hour: 10", "max_actions_per_hour: 11"),
        "quote_slippage_guard.yaml": ('"0.50"', '"0.51"'),
        "card_limit_cap.yaml": ('per_month_max: "600.00"', 'per_month_max: "601.00"'),
    }
    from policy_helpers import DEFAULT_RULES_DIR
    from tessera_core.policy import ruleset_from_yaml_texts

    for file, (old, new) in swaps.items():
        texts = {p.name: p.read_text(encoding="utf-8") for p in DEFAULT_RULES_DIR.glob("*.yaml")}
        assert old in texts[file]
        texts[file] = texts[file].replace(old, new)
        assert ruleset_from_yaml_texts(texts).hash != base


# ---- the whole shipped ruleset ------------------------------------------------------------
@pytest.mark.parametrize("name", ["convert", "pay", "card"])
def test_clean_proposals_are_approved_by_the_whole_shipped_ruleset(name: str) -> None:
    # Day 8: the worst_case_loss_gate expects the simulator's summary for a CONVERT or PAY_SUPPLIER.
    proposal = clean_proposals()[name]
    d = evaluate(proposal, full_context(simulation=sim_for(proposal)), load_ruleset())
    assert d.verdict is Verdict.APPROVE, d.reasons


@pytest.mark.parametrize("action", [ActionType.HOLD, ActionType.FLAG_INVOICE])
def test_actions_that_move_no_money_need_only_the_kill_switch_context(action: ActionType) -> None:
    from tessera_core.policy import PolicyContext

    d = evaluate(make_proposal(action), PolicyContext(kill_switch_engaged=False), load_ruleset())
    assert d.verdict is Verdict.APPROVE, d.reasons

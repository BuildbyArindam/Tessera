"""Day 7 unit tests: the rules that count or compare money (daily cap, concentration, slippage, velocity)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from policy_helpers import NOW, full_context, make_proposal, only, usd
from pydantic import ValidationError
from tessera_core.domain import ActionType, Money, Verdict
from tessera_core.domain.currency import Currency
from tessera_core.policy import POLICY_ERROR, RulesetError, evaluate

CONVERT = ActionType.CONVERT
USD_ONLY_CAP = 'id: daily_cumulative_cap\ntype: daily_cumulative_cap\nparams:\n  daily_cap:\n    USD: "15000.00"\n'


# ---- daily_cumulative_cap -----------------------------------------------------------------
DAILY = only("daily_cumulative_cap.yaml")


@pytest.mark.parametrize(
    ("spent", "amount", "verdict"),
    [
        ("0.00", "15000.00", Verdict.APPROVE),
        ("10000.00", "5000.00", Verdict.APPROVE),
        ("10000.01", "5000.00", Verdict.BLOCK),
        ("0.00", "15000.01", Verdict.BLOCK),
        ("15000.00", "0.01", Verdict.BLOCK),
    ],
)
def test_daily_cap_boundary(spent: str, amount: str, verdict: Verdict) -> None:
    ctx = full_context(spend_today={Currency.USD: usd(spent)})
    assert evaluate(make_proposal(CONVERT, amount), ctx, DAILY).verdict is verdict


def test_daily_cap_stops_a_large_payment_split_into_capped_pieces() -> None:
    spent = Money.zero(Currency.USD)
    verdicts = []
    for _ in range(4):
        d = evaluate(make_proposal(CONVERT, "5000.00"), full_context(spend_today={Currency.USD: spent}), DAILY)
        verdicts.append(d.verdict)
        if d.verdict is Verdict.APPROVE:
            spent = spent + usd("5000.00")
    assert verdicts == [Verdict.APPROVE] * 3 + [Verdict.BLOCK]


def test_daily_cap_is_per_currency() -> None:
    ctx = full_context(spend_today={Currency.USD: usd("15000.00")})
    assert evaluate(make_proposal(CONVERT, "100", "EUR"), ctx, DAILY).verdict is Verdict.APPROVE


def test_daily_cap_blocks_a_currency_with_no_cap() -> None:
    rs = only("daily_cumulative_cap.yaml", USD_ONLY_CAP)
    d = evaluate(make_proposal(CONVERT, "1.00", "EUR"), full_context(), rs)
    assert d.verdict is Verdict.BLOCK and "no daily cap" in d.reasons[0]


def test_daily_cap_missing_spend_context_fails_closed_but_only_for_money_actions() -> None:
    ctx = full_context(spend_today=None)
    d = evaluate(make_proposal(CONVERT), ctx, DAILY)
    assert d.verdict is Verdict.BLOCK and d.reasons[0] == POLICY_ERROR
    assert evaluate(make_proposal(ActionType.HOLD), ctx, DAILY).verdict is Verdict.APPROVE


def test_negative_spend_cannot_be_supplied() -> None:
    with pytest.raises(ValidationError):
        full_context(spend_today={Currency.USD: usd("-5000.00")})


# ---- amount ladder: nothing above a limit may slip through at any magnitude --------------------
LADDER = [
    "5000.01", "9999.99", "19999.99", "20000.00", "20000.01", "32767.00", "65536.00", "99999.99", "1000000.00",
    "2147483648.00", "9007199254740993", "1000000000000.00", "999999999999999.99", "1000000000000000.00",
]  # fmt: skip


@pytest.mark.parametrize("amount", LADDER)
@pytest.mark.parametrize("kind", [ActionType.CONVERT, ActionType.PAY_SUPPLIER, ActionType.TOPUP_CARD])
def test_every_amount_above_the_per_action_cap_is_blocked_by_the_whole_ruleset(kind: ActionType, amount: str) -> None:
    from policy_helpers import clean_proposals
    from tessera_core.policy import load_ruleset

    p = clean_proposals()[
        {ActionType.CONVERT: "convert", ActionType.PAY_SUPPLIER: "pay", ActionType.TOPUP_CARD: "card"}[kind]
    ]
    p = p.model_copy(update={"params": {**p.params, "amount": amount}})
    assert evaluate(p, full_context(), load_ruleset()).verdict is Verdict.BLOCK


@pytest.mark.parametrize("amount", ["1000000000000000.01", "1e15", "9" * 18, 10**16, 10**30])
def test_amounts_beyond_the_sanity_bound_are_blocked(amount: object) -> None:
    assert evaluate(make_proposal(CONVERT, amount, to_currency="EUR"), full_context(), DAILY).verdict is Verdict.BLOCK


# ---- currency_concentration_cap -----------------------------------------------------------
CONC = only("currency_concentration_cap.yaml")  # 40% of the source balance


@pytest.mark.parametrize(
    ("amount", "verdict"),
    [("4000.00", Verdict.APPROVE), ("4000.01", Verdict.BLOCK), ("0.01", Verdict.APPROVE), ("10000.00", Verdict.BLOCK)],
)
def test_concentration_boundary_on_10000_usd(amount: str, verdict: Verdict) -> None:
    p = make_proposal(CONVERT, amount, to_currency="EUR")
    assert evaluate(p, full_context(), CONC).verdict is verdict


def test_concentration_blocks_when_there_is_no_balance_or_a_zero_balance() -> None:
    p = make_proposal(CONVERT, "1.00", "GBP", to_currency="EUR")
    assert evaluate(p, full_context(), CONC).verdict is Verdict.BLOCK  # no GBP balance entry
    zero = full_context(balances={Currency.USD: Money.zero(Currency.USD)})
    assert evaluate(make_proposal(CONVERT, "0.01", to_currency="EUR"), zero, CONC).verdict is Verdict.BLOCK


def test_concentration_only_applies_to_conversions() -> None:
    pay = make_proposal(ActionType.PAY_SUPPLIER, "9000.00", beneficiary_id="sup_old")
    assert evaluate(pay, full_context(balances=None), CONC).verdict is Verdict.APPROVE


def test_concentration_missing_balances_fails_closed_for_a_conversion() -> None:
    d = evaluate(make_proposal(CONVERT, to_currency="EUR"), full_context(balances=None), CONC)
    assert d.verdict is Verdict.BLOCK and d.reasons[0] == POLICY_ERROR


# ---- quote_slippage_guard -----------------------------------------------------------------
SLIP = only("quote_slippage_guard.yaml")  # 0.50% either way; reference USD/EUR 0.9200


def _quote(rate: str, **kwargs: object) -> object:
    return full_context(fx_quote_rate={"USD/EUR": Decimal(rate)}, **kwargs)


@pytest.mark.parametrize(
    ("rate", "verdict"),
    [
        ("0.9200", Verdict.APPROVE),
        ("0.9246", Verdict.APPROVE),  # exactly +0.50%
        ("0.9246001", Verdict.BLOCK),
        ("0.9154", Verdict.APPROVE),  # exactly -0.50%
        ("0.9153999", Verdict.BLOCK),
        ("1.8400", Verdict.BLOCK),  # a quote twice the reference is as suspect as one half of it
    ],
)
def test_slippage_boundary_both_directions(rate: str, verdict: Verdict) -> None:
    p = make_proposal(CONVERT, to_currency="EUR")
    assert evaluate(p, _quote(rate), SLIP).verdict is verdict  # type: ignore[arg-type]


def test_slippage_uses_context_rates_never_the_proposal() -> None:
    p = make_proposal(CONVERT, to_currency="EUR", reference_rate="9.99", rate="9.99", quote_rate="0.9200")
    assert evaluate(p, _quote("0.9500"), SLIP).verdict is Verdict.BLOCK  # type: ignore[arg-type]


@pytest.mark.parametrize("extra", [{}, {"to_currency": "USD"}, {"to_currency": "usd"}, {"to_currency": 5}])
def test_slippage_blocks_a_missing_same_or_malformed_buy_currency(extra: dict[str, object]) -> None:
    d = evaluate(make_proposal(CONVERT, **extra), full_context(), SLIP)
    assert d.verdict is Verdict.BLOCK


def test_slippage_blocks_when_the_pair_has_no_quote_or_reference() -> None:
    p = make_proposal(CONVERT, to_currency="GBP")
    assert evaluate(p, full_context(), SLIP).verdict is Verdict.BLOCK


def test_slippage_missing_rate_context_fails_closed_for_a_conversion_only() -> None:
    ctx = full_context(fx_quote_rate=None)
    d = evaluate(make_proposal(CONVERT, to_currency="EUR"), ctx, SLIP)
    assert d.verdict is Verdict.BLOCK and d.reasons[0] == POLICY_ERROR
    assert evaluate(make_proposal(ActionType.HOLD), ctx, SLIP).verdict is Verdict.APPROVE


# ---- velocity_limit -----------------------------------------------------------------------
VEL = only("velocity_limit.yaml")  # 10 per rolling hour


def _approvals(*minutes_ago: float) -> tuple[object, ...]:
    return tuple(NOW - timedelta(minutes=m) for m in minutes_ago)


def test_velocity_allows_up_to_the_limit_then_blocks() -> None:
    nine = full_context(recent_approval_times=_approvals(*range(1, 10)))  # type: ignore[arg-type]
    ten = full_context(recent_approval_times=_approvals(*range(1, 11)))  # type: ignore[arg-type]
    assert evaluate(make_proposal(), nine, VEL).verdict is Verdict.APPROVE
    assert evaluate(make_proposal(), ten, VEL).verdict is Verdict.BLOCK


def test_velocity_window_is_exactly_one_hour_and_ignores_older_approvals() -> None:
    ctx = full_context(recent_approval_times=_approvals(*([60] * 10), 120, 600))  # type: ignore[arg-type]
    assert evaluate(make_proposal(), ctx, VEL).verdict is Verdict.APPROVE  # 60 min ago is outside
    inside = full_context(recent_approval_times=_approvals(*([59.99] * 10)))  # type: ignore[arg-type]
    assert evaluate(make_proposal(), inside, VEL).verdict is Verdict.BLOCK


def test_velocity_counts_approvals_dated_in_the_future() -> None:
    ctx = full_context(recent_approval_times=_approvals(*([-5] * 10)))  # type: ignore[arg-type]
    assert evaluate(make_proposal(), ctx, VEL).verdict is Verdict.BLOCK


def test_velocity_ignores_actions_that_move_no_money() -> None:
    ten = full_context(recent_approval_times=_approvals(*range(1, 11)))  # type: ignore[arg-type]
    assert evaluate(make_proposal(ActionType.HOLD), ten, VEL).verdict is Verdict.APPROVE
    assert evaluate(make_proposal(ActionType.FLAG_INVOICE), ten, VEL).verdict is Verdict.APPROVE


@pytest.mark.parametrize("missing", ["recent_approval_times", "now"])
def test_velocity_missing_context_fails_closed(missing: str) -> None:
    d = evaluate(make_proposal(), full_context(**{missing: None}), VEL)
    assert d.verdict is Verdict.BLOCK and d.reasons[0] == POLICY_ERROR


def test_velocity_rejects_a_zero_limit_in_yaml() -> None:
    with pytest.raises(RulesetError):
        only("velocity_limit.yaml", "id: velocity_limit\ntype: velocity_limit\nparams:\n  max_actions_per_hour: 0\n")

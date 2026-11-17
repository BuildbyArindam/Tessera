"""The ledger (Day 18): cumulative-limit totals come from approved proposals, never from the world provider.

Regression for the Day 18 red-team finding: five 375,000 INR payments, each inside the per-action cap, all executed
against a 1,200,000 daily cap, because the context the provider built said nothing had been spent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tessera_core.domain import Currency, Money, Proposal
from tessera_core.policy import Ledger, LedgerEntry, PolicyContext, fold, merge_into

NOW = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


def _pay(amount: str, ident: str = "p1", currency: str = "INR") -> Proposal:
    return Proposal(
        id=ident, action_type="PAY_SUPPLIER", rationale="r", confidence=0.5, created_at=NOW,
        params={
            "amount": amount, "currency": currency, "from_currency": currency,
            "beneficiary_id": "BEN-001", "obligation_id": "OBL-1",
        },  # fmt: skip
    )  # fmt: skip


def _card(amount: str, ident: str, vendor: str = "cloud_compute") -> Proposal:
    return Proposal(
        id=ident, action_type="TOPUP_CARD", rationale="r", confidence=0.5, created_at=NOW,
        params={"amount": amount, "currency": "USD", "vendor_id": vendor},
    )  # fmt: skip


def _hold() -> Proposal:
    return Proposal(
        id="h", action_type="HOLD", rationale="r", confidence=0.5, created_at=NOW,
        params={"amount": "1000.00", "currency": "EUR", "to_currency": "INR", "obligation_id": "OBL-2"},
    )  # fmt: skip


def _money(amount: str, currency: Currency = Currency.INR) -> Money:
    return Money(amount=amount, currency=currency)


def test_totals_add_up_per_currency_and_ignore_actions_that_move_no_money() -> None:
    entries = [
        LedgerEntry(_pay("375000.00", "a"), NOW - timedelta(minutes=5)),
        LedgerEntry(_pay("375001.00", "b"), NOW - timedelta(minutes=4)),
        LedgerEntry(_pay("100.00", "c", "EUR"), NOW - timedelta(minutes=3)),
        LedgerEntry(_hold(), NOW - timedelta(minutes=2)),
    ]
    ledger = fold(entries, now=NOW)
    assert ledger.spend_today[Currency.INR] == _money("750001.00")
    assert ledger.spend_today[Currency.EUR] == _money("100.00", Currency.EUR)
    assert len(ledger.recent_approval_times) == 3  # the HOLD is not a money movement


def test_each_window_is_cut_where_its_rule_says() -> None:
    yesterday = NOW - timedelta(days=1)
    two_hours_ago = NOW - timedelta(hours=2)
    last_month = NOW - timedelta(days=40)
    ledger = fold(
        [
            LedgerEntry(_pay("500.00", "y"), yesterday),  # not today, not this hour
            LedgerEntry(_pay("700.00", "t"), two_hours_ago),  # today, not this hour
            LedgerEntry(_card("900.00", "c1"), yesterday),  # this month's card load, not today's
            LedgerEntry(_card("100.00", "c2"), last_month),  # last month: not counted
        ],
        now=NOW,
    )
    assert ledger.spend_today == {Currency.INR: _money("700.00")}
    assert ledger.recent_approval_times == ()
    assert ledger.card_loaded_this_month == {"cloud_compute": _money("900.00", Currency.USD)}


def test_the_day_is_a_utc_day() -> None:
    just_after_midnight = datetime(2026, 3, 2, 0, 30, tzinfo=UTC)
    before = just_after_midnight - timedelta(minutes=40)  # 23:50 the day before
    ledger = fold([LedgerEntry(_pay("500.00"), before)], now=just_after_midnight)
    assert ledger.spend_today == {}
    assert len(ledger.recent_approval_times) == 1  # still inside the hour


def test_the_provider_may_add_to_the_ledger_but_never_lower_it() -> None:
    ledger = fold([LedgerEntry(_pay("1100000.00"), NOW - timedelta(minutes=1))], now=NOW)
    lying = PolicyContext(
        spend_today={}, card_loaded_this_month={}, recent_approval_times=(), now=NOW - timedelta(days=3)
    )
    merged = merge_into(lying, ledger, now=NOW)
    assert merged.spend_today == {Currency.INR: _money("1100000.00")}
    assert len(merged.recent_approval_times) == 1
    assert merged.now == NOW  # the caller's clock, not the provider's

    knows_more = PolicyContext(
        spend_today={Currency.INR: _money("2000000.00")}, card_loaded_this_month={}, recent_approval_times=()
    )
    assert merge_into(knows_more, ledger, now=NOW).spend_today == {Currency.INR: _money("2000000.00")}


def test_a_provider_that_reports_more_recent_approvals_than_the_ledger_keeps_them() -> None:
    provided = tuple(NOW - timedelta(minutes=i) for i in range(1, 8))
    ctx = PolicyContext(spend_today={}, card_loaded_this_month={}, recent_approval_times=provided)
    assert merge_into(ctx, Ledger({}, (), {}), now=NOW).recent_approval_times == provided


def test_an_approval_stamped_in_the_future_still_counts() -> None:
    ledger = fold([LedgerEntry(_pay("500.00"), NOW + timedelta(minutes=1))], now=NOW)
    assert ledger.spend_today == {Currency.INR: _money("500.00")}
    assert ledger.recent_approval_times == (NOW + timedelta(minutes=1),)


def test_amounts_stay_exact() -> None:
    entries = [LedgerEntry(_pay(f"0.{i:02d}", f"p{i}"), NOW) for i in range(1, 11)]
    assert fold(entries, now=NOW).spend_today[Currency.INR].amount == Decimal("0.55")

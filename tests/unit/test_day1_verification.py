"""Day 1 verification checks (a)-(e), kept as permanent regression tests."""

import re
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from tessera_airwallex import MockAirwallex
from tessera_core.domain import Currency, CurrencyMismatchError, Money

CORE = Path(__file__).resolve().parents[2] / "packages" / "core" / "tessera_core"


def test_a_mixed_currency_addition_raises() -> None:
    with pytest.raises(CurrencyMismatchError):
        Money(amount=1, currency=Currency.USD) + Money(amount=1, currency=Currency.EUR)


def test_b_decimal_point_one_plus_point_two_is_exactly_point_three() -> None:
    total = Money(amount=Decimal("0.1"), currency=Currency.USD) + Money(
        amount=Decimal("0.2"), currency=Currency.USD
    )
    assert total == Money(amount=Decimal("0.3"), currency=Currency.USD)
    assert total.amount == Decimal("0.30")


def test_b2_float_literals_are_refused_rather_than_silently_converted() -> None:
    # Money(0.1) would encode binary-float error before we ever see it; reject instead.
    with pytest.raises(ValidationError):
        Money(amount=0.1, currency=Currency.USD)  # type: ignore[arg-type]


def test_c_no_float_on_any_amount_field_in_core() -> None:
    offenders: list[str] = []
    for path in CORE.rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            code = line.split("#", 1)[0]
            annotated_float = re.search(
                r"\b(\w*(amount|price|balance|fee|rate|limit)\w*)\s*:\s*float\b", code
            )
            if annotated_float:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == []


def test_d_same_idempotency_key_changes_balances_once() -> None:
    mock = MockAirwallex({Currency.USD: "1000"})
    quote = mock.create_fx_quote(
        sell=Money(amount=400, currency=Currency.USD), buy_currency=Currency.EUR
    )
    mock.execute_conversion(quote_id=quote.quote_id, idempotency_key="K")
    after_first = {b.currency: b.available.amount for b in mock.get_balances()}
    mock.execute_conversion(quote_id=quote.quote_id, idempotency_key="K")
    after_second = {b.currency: b.available.amount for b in mock.get_balances()}
    assert after_first == after_second
    assert after_second[Currency.USD] == Decimal("600.00")


def test_e_conversion_conserves_value_after_spread() -> None:
    mock = MockAirwallex({Currency.USD: "1000"}, spread_bps=50)
    quote = mock.create_fx_quote(
        sell=Money(amount=777, currency=Currency.USD), buy_currency=Currency.EUR
    )
    mock.execute_conversion(quote_id=quote.quote_id, idempotency_key="K")
    gross_at_mid = Money(
        amount=Decimal(777) * mock.mid_rate(Currency.USD, Currency.EUR), currency=Currency.EUR
    )
    assert quote.buy + mock.fees_collected[Currency.EUR] == gross_at_mid
    assert quote.buy < gross_at_mid  # the customer paid a positive spread

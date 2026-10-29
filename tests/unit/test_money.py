from decimal import Decimal

import pytest
from pydantic import ValidationError

from tessera_core.domain import Currency, CurrencyMismatchError, Money

USD, EUR = Currency.USD, Currency.EUR


def usd(x: str | int | Decimal) -> Money:
    return Money(amount=x, currency=USD)


class TestConstruction:
    def test_quantizes_to_two_places(self) -> None:
        assert usd("1.005").amount == Decimal("1.00")  # half-even: 0 is even
        assert usd("1.015").amount == Decimal("1.02")  # half-even: 2 is even
        assert usd("1.2345").amount == Decimal("1.23")

    def test_int_and_decimal_accepted(self) -> None:
        assert usd(5).amount == Decimal("5.00")
        assert usd(Decimal("5.5")).amount == Decimal("5.50")

    @pytest.mark.parametrize("bad", [0.1, 1.0, True, None, [1], "abc", "", "NaN", "Infinity"])
    def test_rejects_non_decimal_and_non_finite(self, bad: object) -> None:
        with pytest.raises(ValidationError):
            Money(amount=bad, currency=USD)  # type: ignore[arg-type]

    def test_rejects_huge_amount(self) -> None:
        with pytest.raises(ValidationError):
            usd(Decimal("1e30"))
        with pytest.raises(ValidationError):
            usd("1e999999999")

    def test_negative_zero_is_normalised(self) -> None:
        assert str(usd("-0.001").amount) == "0.00"
        assert usd("-0.00") == usd("0")

    def test_immutable_and_no_extra_fields(self) -> None:
        m = usd(1)
        with pytest.raises(ValidationError):
            m.amount = Decimal(2)  # type: ignore[misc]
        with pytest.raises(ValidationError):
            Money(amount=1, currency=USD, tag="x")  # type: ignore[call-arg]


class TestArithmetic:
    def test_decimal_addition_is_exact(self) -> None:
        assert usd("0.1") + usd("0.2") == usd("0.3")

    def test_subtraction_and_negation(self) -> None:
        assert usd("5") - usd("7.50") == usd("-2.50")
        assert -usd("3") == usd("-3")
        assert abs(usd("-3")) == usd("3")

    def test_scaling(self) -> None:
        assert usd("10") * Decimal("0.333") == usd("3.33")
        assert Decimal("2") * usd("10.25") == usd("20.50")
        assert usd("10") * 3 == usd("30")

    def test_scaling_by_float_or_money_is_an_error(self) -> None:
        with pytest.raises(TypeError):
            usd("10") * 0.5  # type: ignore[operator]
        with pytest.raises(TypeError):
            usd("10") * usd("2")  # type: ignore[operator]

    def test_mixed_currency_arithmetic_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            usd(1) + Money(amount=1, currency=EUR)
        with pytest.raises(CurrencyMismatchError):
            usd(1) - Money(amount=1, currency=EUR)

    def test_adding_a_plain_number_is_an_error(self) -> None:
        with pytest.raises(TypeError):
            usd(1) + 1  # type: ignore[operator]


class TestComparison:
    def test_ordering_same_currency(self) -> None:
        assert usd(1) < usd(2)
        assert usd(2) <= usd(2)
        assert usd(3) > usd(2)
        assert usd(2) >= usd(2)

    def test_ordering_across_currencies_raises(self) -> None:
        for op in ("__lt__", "__le__", "__gt__", "__ge__"):
            with pytest.raises(CurrencyMismatchError):
                getattr(usd(1), op)(Money(amount=1, currency=EUR))

    def test_equality_across_currencies_is_false_not_an_error(self) -> None:
        assert usd(1) != Money(amount=1, currency=EUR)

    def test_hashable_and_equal_values_share_a_hash(self) -> None:
        assert hash(usd("1.0")) == hash(usd("1.00"))
        assert len({usd(1), usd("1.00")}) == 1


def test_serialises_amount_as_string_not_float() -> None:
    dumped = usd("12.30").model_dump(mode="json")
    assert dumped == {"amount": "12.30", "currency": "USD"}
    assert Money.model_validate_json('{"amount": "12.30", "currency": "USD"}') == usd("12.30")


def test_json_float_amount_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Money.model_validate_json('{"amount": 12.3, "currency": "USD"}')


def test_zero_and_predicates() -> None:
    z = Money.zero(Currency.INR)
    assert z.is_zero() and not z.is_positive() and not z.is_negative()
    assert usd(-1).is_negative() and usd(1).is_positive()

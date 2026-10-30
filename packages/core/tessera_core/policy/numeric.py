"""Strict numbers for rule configuration, and exact arithmetic for rule checks.

Rule limits come from YAML, where `5000.5` parses as a float and `0.1` is already wrong. Every
decimal parameter is therefore accepted only as a quoted string, an int or a Decimal, is never
silently rounded (more decimal places than allowed is an error), and is stored in one canonical
form so equal limits hash equally.
"""

from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator

from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money

# 60 significant digits: exact for any amount Money accepts (at most 17 digits) times a rule limit.
EXACT = Context(prec=60, rounding=ROUND_HALF_EVEN)
_PARAM_BOUND = Decimal(10) ** 6


def _strict_decimal(value: object, places: int) -> Decimal:
    if isinstance(value, bool | float):
        raise ValueError('write the number as a quoted string such as "50.00"; floats are not allowed')
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int | str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise ValueError("not a valid decimal number") from None
    else:
        raise ValueError("must be a string, integer or Decimal")
    if not parsed.is_finite() or parsed.copy_abs() > _PARAM_BOUND:
        raise ValueError("must be a finite number no larger than 1000000")
    quantum = Decimal(1).scaleb(-places)
    quantized = parsed.quantize(quantum, context=EXACT)
    if quantized != parsed:
        raise ValueError(f"has more than {places} decimal places")
    return quantized


def _percent(value: object) -> Decimal:
    return _strict_decimal(value, 2)


def _ratio(value: object) -> Decimal:
    return _strict_decimal(value, 4)


def _check_percent(value: Decimal) -> Decimal:
    if not (0 < value <= 100):
        raise ValueError("must be greater than 0 and at most 100")
    return value


def _check_positive(value: Decimal) -> Decimal:
    if value <= 0:
        raise ValueError("must be greater than 0")
    return value


# A percentage, 0 < x <= 100, at most 2 decimal places ("50.00" means fifty percent).
Percent = Annotated[Decimal, BeforeValidator(_percent), AfterValidator(_check_percent)]
# A positive ratio with at most 4 decimal places ("0.5000" means half).
Ratio = Annotated[Decimal, BeforeValidator(_ratio), AfterValidator(_check_positive)]


def _wrap_caps(value: Any) -> Any:
    if not isinstance(value, dict):
        raise ValueError("must map a currency code to an amount")
    # Going through Money rejects floats, NaN and absurd values exactly as proposal amounts are.
    return {key: {"amount": amount, "currency": key} for key, amount in value.items()}


def _check_caps(value: dict[Currency, Money]) -> dict[Currency, Money]:
    if not value:
        raise ValueError("must define at least one currency")
    for currency, cap in value.items():
        if not cap.is_positive():
            raise ValueError(f"limit for {currency.value} must be strictly positive")
    return value


# {"USD": "15000.00", ...}: a positive Money limit per currency.
CurrencyLimits = Annotated[dict[Currency, Money], BeforeValidator(_wrap_caps), AfterValidator(_check_caps)]

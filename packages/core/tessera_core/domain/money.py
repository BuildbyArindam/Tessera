"""Money: a Decimal amount in exactly one currency.

Design rules (see Master Context rule 5):
- Floats are rejected at construction, not silently converted. `Money(0.1, ...)` is an error
  because the float 0.1 is already wrong before we see it. Pass a str, int or Decimal.
- Amounts are quantized to 2 dp with banker's rounding (ROUND_HALF_EVEN), which avoids a
  systematic upward bias when many small amounts are rounded.
- Arithmetic runs in a 60-digit context so intermediate results are exact for any amount
  we accept, then is quantized once on construction.
- Mixing currencies is an error. There is deliberately no implicit conversion.
- |amount| is capped at MAX_ABS_AMOUNT. This is a sanity bound that turns absurd inputs
  (and Decimal overflow tricks) into a validation error instead of a downstream surprise.
"""

from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation

from pydantic import field_validator

from tessera_core.domain.base import DomainModel
from tessera_core.domain.currency import Currency

_QUANTUM = Decimal("0.01")
_CTX = Context(prec=60, rounding=ROUND_HALF_EVEN)
MAX_ABS_AMOUNT = Decimal(10) ** 15


class CurrencyMismatchError(ValueError):
    """Raised when an operation mixes two different currencies."""


class Money(DomainModel):
    amount: Decimal
    currency: Currency

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce_amount(cls, value: object) -> Decimal:
        if isinstance(value, bool | float):
            raise ValueError(
                "amount must be a Decimal, int or str; floats and bools are not allowed for money"
            )
        if isinstance(value, Decimal):
            parsed = value
        elif isinstance(value, int | str):
            try:
                parsed = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError(f"invalid decimal amount: {value!r}") from exc
        else:
            raise ValueError(f"unsupported amount type: {type(value).__name__}")

        if not parsed.is_finite():
            raise ValueError("amount must be finite")
        # copy_abs() ignores the decimal context; abs() would raise Overflow on huge exponents.
        if parsed.copy_abs() > MAX_ABS_AMOUNT:
            raise ValueError(f"amount exceeds the sanity bound of {MAX_ABS_AMOUNT}")

        try:
            quantized = parsed.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN, context=_CTX)
        except InvalidOperation as exc:  # pragma: no cover - guarded by the bound above
            raise ValueError(f"cannot quantize amount: {value!r}") from exc
        return Decimal("0.00") if quantized == 0 else quantized  # normalise -0.00

    # ------------------------------------------------------------------ constructors
    @classmethod
    def zero(cls, currency: Currency) -> "Money":
        return cls(amount=Decimal(0), currency=currency)

    # ------------------------------------------------------------------ predicates
    def is_zero(self) -> bool:
        return self.amount == 0

    def is_positive(self) -> bool:
        return self.amount > 0

    def is_negative(self) -> bool:
        return self.amount < 0

    # ------------------------------------------------------------------ arithmetic
    def _require_same_currency(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"cannot combine {self.currency.value} with {other.currency.value}"
            )

    def __add__(self, other: object) -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(amount=_CTX.add(self.amount, other.amount), currency=self.currency)

    def __sub__(self, other: object) -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(amount=_CTX.subtract(self.amount, other.amount), currency=self.currency)

    def __neg__(self) -> "Money":
        return Money(amount=-self.amount, currency=self.currency)

    def __abs__(self) -> "Money":
        return Money(amount=abs(self.amount), currency=self.currency)

    def __mul__(self, factor: object) -> "Money":
        """Scale by a Decimal or int. Floats and Money are rejected."""
        if isinstance(factor, bool | float):
            raise TypeError("scale Money by a Decimal or int, never a float or bool")
        if not isinstance(factor, Decimal | int):
            return NotImplemented
        return Money(amount=_CTX.multiply(self.amount, factor), currency=self.currency)

    __rmul__ = __mul__

    # ------------------------------------------------------------------ ordering
    # Equality is the pydantic default (same currency and amount). Ordering across
    # currencies is meaningless, so it raises instead of guessing.
    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.amount <= other.amount

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.amount >= other.amount

    def __str__(self) -> str:
        return f"{self.amount} {self.currency.value}"

"""SimulationConfig: the knobs a simulation takes from the caller, never from a proposal.

The spread and fee describe what a conversion costs. They are assumptions (the Day 1 mock charges
50 bps, so that is the default), not measured Airwallex prices; Day 17 reports must say so.
Everything is validated on construction so a bad number fails loudly before any path is drawn.
"""

from decimal import Decimal, InvalidOperation

from pydantic import Field, field_validator

from tessera_core.domain.base import DomainModel

DEFAULT_SPREAD_BPS = Decimal(50)


class SimulationConfig(DomainModel):
    # Length of each resampled run of consecutive returns, in business days (5 = one week).
    block_length: int = Field(default=5, ge=1, le=60)
    # Cost of one conversion as basis points of the converted value (50 = 0.50%).
    spread_bps: Decimal = DEFAULT_SPREAD_BPS
    # Fixed cost of one conversion, in the SELL currency. Zero by default.
    fee: Decimal = Decimal("0.00")
    # Remove the historical drift before resampling, so waiting is judged on risk, not on a trend.
    demean: bool = True

    @field_validator("spread_bps", "fee", mode="before")
    @classmethod
    def _no_floats(cls, value: object) -> Decimal:
        if isinstance(value, bool | float):
            raise ValueError("write the number as a Decimal, int or quoted string; floats are not allowed")
        if isinstance(value, Decimal):
            return value
        if isinstance(value, int | str):
            try:
                return Decimal(value)
            except InvalidOperation:
                raise ValueError("not a valid decimal number") from None
        raise ValueError("must be a Decimal, int or string")

    @field_validator("spread_bps")
    @classmethod
    def _spread_in_range(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or not (Decimal(0) <= value < Decimal(10_000)):
            raise ValueError("spread_bps must be finite and in [0, 10000)")
        return value

    @field_validator("fee")
    @classmethod
    def _fee_not_negative(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("fee must be finite and not negative")
        return value

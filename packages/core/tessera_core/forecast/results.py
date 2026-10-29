"""Forecast result type.

Money-like values (point/lower/upper) are Decimal quantized to 2 dp so they can
flow into Money(...) later without float drift (master rule 5). The statistical
internals of the models use float arrays; conversion happens only at this
boundary in `to_decimal`.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_TWO_DP = Decimal("0.01")


def to_decimal(value: float) -> Decimal:
    """Convert a model float to a 2 dp Decimal via repr (no binary-float artefacts)."""
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"non-finite forecast value: {value!r}")
    return Decimal(repr(float(value))).quantize(_TWO_DP, rounding=ROUND_HALF_EVEN)


class ForecastResult(BaseModel):
    """A calibrated interval forecast for one business, currency and horizon."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_id: str
    currency: str  # TODO(day1-repo): switch to tessera_core.domain.Currency once wired in
    horizon_weeks: int = Field(gt=0)
    point: Decimal
    lower: Decimal
    upper: Decimal
    target_coverage: float = Field(gt=0.0, lt=1.0)
    model_version: str

    @model_validator(mode="after")
    def _ordered(self) -> ForecastResult:
        if not (self.lower <= self.point <= self.upper):
            raise ValueError(
                f"interval must satisfy lower <= point <= upper, got {self.lower} <= {self.point} <= {self.upper}"
            )
        return self

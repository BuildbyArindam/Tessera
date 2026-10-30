"""PolicyContext: the read-only world a decision is made in.

Every field is optional on purpose: `None` means "the caller did not supply it".
A rule declares which fields it needs (`Rule.required_context`) and the engine fails closed
(BLOCK, policy_error) when a needed field is `None`. Nothing here is fetched lazily; the
caller assembles a snapshot, so a decision can be replayed from the audit log.
"""

from decimal import Decimal
from enum import IntEnum
from typing import Annotated

from pydantic import Field, StrictBool, field_validator

from tessera_core.domain.base import DomainModel
from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money

_PAIR_PATTERN = r"^[A-Z]{3}/[A-Z]{3}$"
PairKey = Annotated[str, Field(pattern=_PAIR_PATTERN)]


class AutonomyLevel(IntEnum):
    """L0 advise-only .. L3 extended. Behaviour per level lands on Day 14."""

    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3


class ForecastConfidence(DomainModel):
    """How wide the forecast interval is for one currency, relative to its point estimate.

    Used by the Day 7 forecast_confidence_gate. A ratio, not money, hence Decimal not Money.
    """

    relative_width: Decimal = Field(ge=0)


class PolicyContext(DomainModel):
    balances: dict[Currency, Money] | None = None
    spend_today: dict[Currency, Money] | None = None
    beneficiary_allowlist: frozenset[str] | None = None
    forecast_confidence: dict[Currency, ForecastConfidence] | None = None
    # "SELL/BUY" -> buy units per 1 sell unit, the same convention as FxQuote.rate.
    fx_reference_rate: dict[PairKey, Decimal] | None = None
    autonomy_level: AutonomyLevel | None = None
    # Runtime state, not rule config: an operator flips it without redeploying rules.
    kill_switch_engaged: StrictBool | None = None

    @field_validator("balances", "spend_today")
    @classmethod
    def _keys_match_money_currency(cls, value: dict[Currency, Money] | None) -> dict[Currency, Money] | None:
        if value is not None:
            for currency, money in value.items():
                if money.currency != currency:
                    raise ValueError(f"entry keyed {currency.value} holds {money.currency.value}")
        return value

    @field_validator("fx_reference_rate")
    @classmethod
    def _rates_positive(cls, value: dict[str, Decimal] | None) -> dict[str, Decimal] | None:
        if value is not None:
            for pair, rate in value.items():
                if not rate.is_finite() or rate <= 0:
                    raise ValueError(f"reference rate for {pair} must be positive and finite")
        return value

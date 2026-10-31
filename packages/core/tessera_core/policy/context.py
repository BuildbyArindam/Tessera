"""PolicyContext: the read-only world a decision is made in.

Every field is optional on purpose: `None` means "the caller did not supply it".
A rule declares which fields it needs (`Rule.required_context`) and the engine fails closed
(BLOCK, policy_error) when a needed field is `None`. Nothing here is fetched lazily; the
caller assembles a snapshot, so a decision can be replayed from the audit log.
"""

from decimal import Decimal
from enum import IntEnum
from typing import Annotated, TypeVar

from pydantic import AwareDatetime, Field, StrictBool, field_validator

from tessera_core.domain.base import DomainModel
from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money
from tessera_core.domain.simulation import SimulationSummary

_PAIR_PATTERN = r"^[A-Z]{3}/[A-Z]{3}$"
PairKey = Annotated[str, Field(pattern=_PAIR_PATTERN)]


T = TypeVar("T")


class MissingContextError(LookupError):
    """A rule reached for a context field the caller did not supply. The engine turns this into
    BLOCK / policy_error, so a rule never has to guess a default for missing data."""


def require(value: T | None, name: str) -> T:
    if value is None:
        raise MissingContextError(name)
    return value


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
    # The decision time. Rules never read a clock; the caller supplies it, so a decision replays.
    now: AwareDatetime | None = None
    # Beneficiary id -> when it was added. An id on the allowlist but absent here has unknown age.
    beneficiary_added_at: dict[str, AwareDatetime] | None = None
    # When each recently approved money-moving action was approved (any age; rules window it).
    recent_approval_times: tuple[AwareDatetime, ...] | None = None
    # "SELL/BUY" -> the rate of the executable quote, same convention as fx_reference_rate.
    fx_quote_rate: dict[PairKey, Decimal] | None = None
    # Vendor id -> value already loaded onto that vendor's card this calendar month.
    card_loaded_this_month: dict[str, Money] | None = None
    # The simulator's verdict on THIS proposal (Day 8), built by the caller from simulate(...).
    simulation: SimulationSummary | None = None

    @field_validator("balances", "spend_today")
    @classmethod
    def _keys_match_money_currency(cls, value: dict[Currency, Money] | None) -> dict[Currency, Money] | None:
        if value is not None:
            for currency, money in value.items():
                if money.currency != currency:
                    raise ValueError(f"entry keyed {currency.value} holds {money.currency.value}")
        return value

    @field_validator("spend_today")
    @classmethod
    def _spend_not_negative(cls, value: dict[Currency, Money] | None) -> dict[Currency, Money] | None:
        # A negative total would lower what a cap counts and let a larger action through.
        if value is not None and any(money.is_negative() for money in value.values()):
            raise ValueError("spend_today must not contain negative amounts")
        return value

    @field_validator("card_loaded_this_month")
    @classmethod
    def _card_loads_not_negative(cls, value: dict[str, Money] | None) -> dict[str, Money] | None:
        if value is not None and any(money.is_negative() for money in value.values()):
            raise ValueError("card_loaded_this_month must not contain negative amounts")
        return value

    @field_validator("fx_reference_rate", "fx_quote_rate")
    @classmethod
    def _rates_positive(cls, value: dict[str, Decimal] | None) -> dict[str, Decimal] | None:
        if value is not None:
            for pair, rate in value.items():
                if not rate.is_finite() or rate <= 0:
                    raise ValueError(f"reference rate for {pair} must be positive and finite")
        return value

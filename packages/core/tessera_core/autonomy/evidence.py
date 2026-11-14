"""Evidence: the timestamped observations the controller judges, and the rolling window over them.

The controller never fetches anything. A collector (outside this package) turns what happened into these records and
hands the controller an `Evidence` bundle, so an assessment can be replayed from the same inputs.

Four kinds of observation, one per promotion or demotion criterion:
  ViolationObservation  an action that broke a policy executed (zero tolerance)
  ShadowObservation     the agent's proposal compared with what a person decided (agreed or not)
  SavingObservation     realised FX saving of one decision versus the baseline, per 10,000 units converted
  CoverageObservation   whether one realised forecast value fell inside its interval, and the interval's target

Amounts are Decimal and strict: a float is rejected, as everywhere money or a money-derived figure is handled.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Annotated

from pydantic import AwareDatetime, Field, StrictBool

from tessera_core.domain.base import DomainModel
from tessera_core.policy.context import AutonomyLevel

_CODE = r"^[a-z][a-z_]{0,39}$"
_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_BOUND = Decimal(10) ** 9

StrictDecimal = Annotated[Decimal, Field(strict=True, allow_inf_nan=False, gt=-_BOUND, lt=_BOUND)]
Target = Annotated[Decimal, Field(strict=True, allow_inf_nan=False, gt=0, lt=1)]


class ViolationObservation(DomainModel):
    at: AwareDatetime
    kind: str = Field(pattern=_CODE)
    proposal_id: str | None = Field(default=None, pattern=_ID)


class ShadowObservation(DomainModel):
    at: AwareDatetime
    agreed: StrictBool


class SavingObservation(DomainModel):
    at: AwareDatetime
    saving_per_10k: StrictDecimal


class CoverageObservation(DomainModel):
    at: AwareDatetime
    covered: StrictBool
    target: Target


class Evidence(DomainModel):
    violations: tuple[ViolationObservation, ...] = ()
    shadow: tuple[ShadowObservation, ...] = ()
    savings: tuple[SavingObservation, ...] = ()
    coverage: tuple[CoverageObservation, ...] = ()


class AutonomyState(DomainModel):
    """The current level and when it began. `since` bounds which evidence counts for the next step."""

    level: AutonomyLevel
    since: AwareDatetime


def in_window(at: AwareDatetime, now: AwareDatetime, days: int, since: AwareDatetime | None = None) -> bool:
    """True if `at` is no later than `now`, no earlier than `days` ago, and (when given) strictly after `since`.

    Evidence from the future cannot count in favour: only what has already happened vouches for the agent.
    """
    if at > now or at < now - timedelta(days=days):
        return False
    return since is None or at > since

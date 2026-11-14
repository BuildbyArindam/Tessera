"""Builders for the Day 14 autonomy tests: a controller on the shipped config, and evidence that is good or not."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tessera_core.autonomy import (
    AutonomyController,
    AutonomyLevel,
    AutonomyState,
    CoverageObservation,
    Evidence,
    SavingObservation,
    ShadowObservation,
    ViolationObservation,
    load_autonomy_config,
)

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
CONFIG = load_autonomy_config()
CONTROLLER = AutonomyController(CONFIG)


def state(level: AutonomyLevel = AutonomyLevel.L1, days_ago: int = 10) -> AutonomyState:
    return AutonomyState(level=level, since=NOW - timedelta(days=days_ago))


def _times(n: int) -> list[datetime]:
    """n distinct moments in the last day, so all of them fall after a `since` of a few days ago."""
    return [NOW - timedelta(minutes=10 * (i + 1)) for i in range(n)]


def shadow(n: int = 40, agreed: int = 36) -> tuple[ShadowObservation, ...]:
    return tuple(ShadowObservation(at=t, agreed=i < agreed) for i, t in enumerate(_times(n)))


def savings(n: int = 25, low: str = "10.00", high: str = "15.00") -> tuple[SavingObservation, ...]:
    values = [Decimal(low), Decimal(high)]
    return tuple(SavingObservation(at=t, saving_per_10k=values[i % 2]) for i, t in enumerate(_times(n)))


def coverage(n: int = 40, covered: int = 32, target: str = "0.80") -> tuple[CoverageObservation, ...]:
    return tuple(
        CoverageObservation(at=t, covered=i < covered, target=Decimal(target)) for i, t in enumerate(_times(n))
    )


def good_evidence() -> Evidence:
    """Enough clean evidence for every criterion, all dated after a level that began 10 days ago."""
    return Evidence(shadow=shadow(), savings=savings(), coverage=coverage())


def violation(at: datetime | None = None) -> ViolationObservation:
    return ViolationObservation(at=at or NOW - timedelta(hours=1), kind="executed_without_approval")

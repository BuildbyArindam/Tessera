"""Queries for the autonomy dial (Day 14): the one-row level state and the promotions the controller proposes.

Like repo.py: every function takes the caller's Session and never commits, so a level change commits (or rolls back)
together with the audit event that describes it. The level is read from here and from nowhere else: the policy engine's
`autonomy_level` is overlaid from this row, never taken from a proposal, a world provider or model output.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tessera_core.autonomy import AutonomyLevel, AutonomyState

from tessera_store import models as m

STATE_ID = 1


def get_state_row(session: Session, *, lock: bool = False) -> m.AutonomyState:
    """The state row, created at L0 (advise-only) if it is somehow missing. `lock=True` is SELECT ... FOR UPDATE."""
    row = session.get(m.AutonomyState, STATE_ID, with_for_update=lock)
    if row is None:
        session.execute(
            pg_insert(m.AutonomyState)
            .values(id=STATE_ID, level=int(AutonomyLevel.L0), since=func.now())
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.flush()
        row = session.get(m.AutonomyState, STATE_ID, with_for_update=lock, populate_existing=True)
        assert row is not None
    return row


def to_state(row: m.AutonomyState) -> AutonomyState:
    return AutonomyState(level=AutonomyLevel(row.level), since=row.since)


def get_state(session: Session) -> AutonomyState:
    return to_state(get_state_row(session))


def get_level(session: Session) -> AutonomyLevel:
    return AutonomyLevel(get_state_row(session).level)


def set_level(session: Session, row: m.AutonomyState, level: AutonomyLevel, at: datetime) -> AutonomyLevel:
    """Change the level and restart the evidence clock. Returns the old level. The caller holds the row lock."""
    old = AutonomyLevel(row.level)
    row.level, row.since, row.updated_at = int(level), at, at
    session.flush()
    return old


# ---- promotions ----------------------------------------------------------------------------------
def get_pending_promotion(session: Session, *, lock: bool = False) -> m.AutonomyPromotion | None:
    stmt = select(m.AutonomyPromotion).where(m.AutonomyPromotion.status == "PENDING")
    return session.scalars(stmt.with_for_update() if lock else stmt).first()


def get_promotion(session: Session, promotion_id: int, *, lock: bool = False) -> m.AutonomyPromotion | None:
    return session.get(m.AutonomyPromotion, promotion_id, with_for_update=lock)


def create_promotion(
    session: Session, *, from_level: AutonomyLevel, to_level: AutonomyLevel, assessment: dict[str, Any], at: datetime
) -> m.AutonomyPromotion:
    row = m.AutonomyPromotion(
        from_level=int(from_level), to_level=int(to_level), status="PENDING", assessment=assessment, proposed_at=at
    )
    session.add(row)
    session.flush()
    return row


def close_promotion(
    session: Session, row: m.AutonomyPromotion, status: str, *, at: datetime, actor: str, note: str | None = None
) -> None:
    """PENDING -> CONFIRMED | REJECTED | CANCELLED, once. Any other move is a bug and raises."""
    if row.status != "PENDING":
        raise ValueError(f"promotion {row.id} is {row.status}, not PENDING")
    if status not in ("CONFIRMED", "REJECTED", "CANCELLED"):
        raise ValueError(f"{status!r} is not a way to close a promotion")
    row.status, row.decided_at, row.actor, row.note = status, at, actor, note
    session.flush()


def list_promotions(session: Session, *, limit: int = 20) -> Sequence[m.AutonomyPromotion]:
    stmt = select(m.AutonomyPromotion).order_by(m.AutonomyPromotion.id.desc()).limit(limit)
    return session.scalars(stmt).all()

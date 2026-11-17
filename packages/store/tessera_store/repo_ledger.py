"""The approved-money ledger (Day 18): which money-moving proposals count against today's limits, read from the rows.

`daily_cumulative_cap`, `velocity_limit` and `card_limit_cap` are only as strong as the totals they are given.
The totals are computed here, from the proposals themselves, by `tessera_core.policy.ledger.fold`; no world
provider is trusted to supply them. A proposal counts from the moment it is approved (by policy at planning
time, or by a person) until it
fails or is rejected: APPROVED, SUBMITTED and SETTLED count, so "approved but not yet executed" is inside the total.

Like repo.py: every function takes the caller's Session and never commits.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func, select, text
from sqlalchemy.orm import Session
from tessera_core.domain import Proposal as ProposalModel
from tessera_core.policy import LedgerEntry

from tessera_store import models as m

MONEY_MOVING = ("CONVERT", "PAY_SUPPLIER", "TOPUP_CARD")
COUNTED_STATUSES = ("APPROVED", "SUBMITTED", "SETTLED")


def lock_business_decisions(session: Session, business_id: str) -> None:
    """Serialise decisions that read the ledger for one business, until the transaction ends.

    Without it two planning runs at the same instant each see the same empty ledger, and each approves a payment that
    is inside the cap on its own and over it together. A transaction-scoped advisory lock: released by commit or
    rollback.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"tessera:ledger:{business_id}"}
    )


def entries(session: Session, business_id: str, *, since: datetime, exclude_id: str | None = None) -> list[LedgerEntry]:
    """Counted proposals approved at or after `since`. `exclude_id` leaves out the proposal being judged, so a re-check
    at execution time does not count the action against itself.

    When a proposal was approved: a person's approval carries the database's timestamp (the same clock that stamps every
    policy decision, which approval coverage compares it with, so it is deliberately not re-stamped from the
    application's clock); an automatic approval is the proposal's own `proposed_at`. An approval stamped later than
    `now` is still counted, which is the conservative reading of a clock fault, the same one velocity_limit takes.
    """
    approved_at = func.coalesce(
        select(m.Approval.decided_at)
        .where(and_(m.Approval.proposal_id == m.Proposal.id, m.Approval.decision == "APPROVED"))
        .scalar_subquery(),
        m.Proposal.proposed_at,
    )
    stmt = select(m.Proposal, approved_at.label("approved_at")).where(
        m.Proposal.business_id == business_id,
        m.Proposal.action_type.in_(MONEY_MOVING),
        m.Proposal.status.in_(COUNTED_STATUSES),
        approved_at >= since,
    )
    if exclude_id is not None:
        stmt = stmt.where(m.Proposal.id != exclude_id)
    return [
        LedgerEntry(
            proposal=ProposalModel(
                id=row.id,
                action_type=row.action_type,
                params=row.params,
                rationale=row.rationale,
                confidence=row.confidence,
                created_at=row.proposed_at,
            ),
            approved_at=at,
        )
        for row, at in session.execute(stmt)
    ]

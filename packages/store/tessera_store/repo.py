"""Small, explicit queries. No business rules live here: routes and services decide, this module reads and writes.

Every function takes the caller's Session and never commits, so a caller can put several writes (and the audit
events that describe them) in one transaction.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tessera_core.domain import Invoice as InvoiceModel
from tessera_core.domain import Proposal as ProposalModel
from tessera_core.forecast import ForecastResult

from tessera_store import models as m

UNIQUE_VIOLATION = "23505"


class DuplicateError(Exception):
    """The row's primary key (or another unique key) already exists."""


def _pgcode(exc: IntegrityError) -> str | None:
    return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)


# ---- businesses and invoices ----------------------------------------------------------------------
def get_business(session: Session, business_id: str) -> m.Business | None:
    return session.get(m.Business, business_id)


def add_invoice(session: Session, invoice: InvoiceModel) -> m.Invoice:
    row = m.Invoice(
        id=invoice.id,
        business_id=invoice.business_id,
        customer_id=invoice.customer_id,
        amount=invoice.amount.amount,
        currency=invoice.amount.currency.value,
        issued_on=invoice.issued_on,
        due_on=invoice.due_on,
        status=invoice.status.value,
        notes=invoice.notes,
    )
    try:
        with session.begin_nested():  # a savepoint: a duplicate must not poison the caller's transaction
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        if _pgcode(exc) == UNIQUE_VIOLATION:
            raise DuplicateError(invoice.id) from exc
        raise
    return row


# ---- forecasts --------------------------------------------------------------------------------------
def save_forecasts(session: Session, as_of: date, results: Iterable[ForecastResult]) -> list[m.Forecast]:
    """Insert forecasts, skipping any that already exist for the same business, currency, horizon, date, model."""
    rows = [
        {
            "business_id": r.business_id,
            "currency": r.currency,
            "horizon_weeks": r.horizon_weeks,
            "as_of": as_of,
            "point": r.point,
            "lower": r.lower,
            "upper": r.upper,
            "target_coverage": r.target_coverage,
            "model_version": r.model_version,
        }
        for r in results
    ]
    if not rows:
        return []
    stmt = pg_insert(m.Forecast).values(rows).on_conflict_do_nothing(constraint="uq_forecasts_natural_key")
    ids = session.execute(stmt.returning(m.Forecast.id)).scalars().all()
    return list(session.scalars(select(m.Forecast).where(m.Forecast.id.in_(ids)).order_by(m.Forecast.id)))


def list_forecasts(
    session: Session, *, business_id: str | None, currency: str | None, limit: int, offset: int
) -> Sequence[m.Forecast]:
    stmt = select(m.Forecast)
    if business_id is not None:
        stmt = stmt.where(m.Forecast.business_id == business_id)
    if currency is not None:
        stmt = stmt.where(m.Forecast.currency == currency)
    stmt = stmt.order_by(m.Forecast.as_of.desc(), m.Forecast.currency, m.Forecast.horizon_weeks, m.Forecast.id)
    return session.scalars(stmt.limit(limit).offset(offset)).all()


# ---- proposals and decisions ---------------------------------------------------------------------
def get_proposal(session: Session, proposal_id: str, *, lock: bool = False) -> m.Proposal | None:
    return session.get(m.Proposal, proposal_id, with_for_update=lock)


def insert_proposal_if_new(
    session: Session,
    proposal: ProposalModel,
    *,
    business_id: str,
    status: str,
    plan_record: dict[str, object] | None,
) -> bool:
    """True if inserted, False if a proposal with this id already exists (deterministic ids make re-planning safe)."""
    stmt = (
        pg_insert(m.Proposal)
        .values(
            id=proposal.id,
            business_id=business_id,
            action_type=proposal.action_type.value,
            params=proposal.params,
            rationale=proposal.rationale,
            confidence=proposal.confidence,
            status=status,
            plan_record=plan_record,
            proposed_at=proposal.created_at,
        )
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(m.Proposal.id)
    )
    return session.execute(stmt).scalar_one_or_none() is not None


def add_decision(
    session: Session,
    *,
    proposal_id: str,
    phase: str,
    verdict: str,
    reasons: Sequence[str],
    rule_ids: Sequence[str],
    ruleset_hash: str | None,
    context: dict[str, object] | None,
) -> m.PolicyDecision:
    row = m.PolicyDecision(
        proposal_id=proposal_id,
        phase=phase,
        verdict=verdict,
        reasons=list(reasons),
        rule_ids=list(rule_ids),
        ruleset_hash=ruleset_hash,
        context=context,
    )
    session.add(row)
    session.flush()
    return row


def list_proposals(
    session: Session, *, business_id: str | None, status: str | None, limit: int, offset: int
) -> Sequence[m.Proposal]:
    stmt = select(m.Proposal)
    if business_id is not None:
        stmt = stmt.where(m.Proposal.business_id == business_id)
    if status is not None:
        stmt = stmt.where(m.Proposal.status == status)
    stmt = stmt.order_by(m.Proposal.created_at.desc(), m.Proposal.id)
    return session.scalars(stmt.limit(limit).offset(offset)).all()


def latest_decisions(session: Session, proposal_ids: Sequence[str]) -> dict[str, m.PolicyDecision]:
    """The most recent policy decision for each proposal id."""
    if not proposal_ids:
        return {}
    rows = session.scalars(
        select(m.PolicyDecision).where(m.PolicyDecision.proposal_id.in_(proposal_ids)).order_by(m.PolicyDecision.id)
    )
    return {row.proposal_id: row for row in rows}  # ascending id, so the last write per proposal wins


def approvals_for(session: Session, proposal_ids: Sequence[str]) -> dict[str, m.Approval]:
    if not proposal_ids:
        return {}
    rows = session.scalars(select(m.Approval).where(m.Approval.proposal_id.in_(proposal_ids)))
    return {row.proposal_id: row for row in rows}


def record_human_decision(
    session: Session, proposal: m.Proposal, *, decision: str, actor: str, note: str | None, new_status: str
) -> m.Approval:
    """Write the approval row and move the proposal. The caller holds the row lock and checked the old status."""
    row = m.Approval(proposal_id=proposal.id, decision=decision, actor=actor, note=note)
    session.add(row)
    proposal.status = new_status
    session.flush()
    return row

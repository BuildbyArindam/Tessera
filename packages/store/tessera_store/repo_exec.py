"""Queries for the execution layer: executions, webhook events, and guarded status moves.

Like repo.py: every function takes the caller's Session and never commits. Status changes go through the lifecycle
tables in tessera_core, so an illegal move raises IllegalTransitionError instead of being written.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tessera_core.domain.lifecycle import transition_action, transition_execution

from tessera_store import models as m


# ---- guarded moves --------------------------------------------------------------------------------
def move_proposal(session: Session, proposal: m.Proposal, target: str) -> str:
    """Move a proposal along ACTION_TRANSITIONS. Returns the old status. Raises IllegalTransitionError."""
    old = proposal.status
    proposal.status = transition_action(old, target).value
    session.flush()
    return old


def move_execution(
    session: Session,
    execution: m.Execution,
    target: str,
    *,
    external_id: str | None = None,
    result: dict[str, Any] | None = None,
    at: datetime,
) -> str:
    """Move an execution along EXECUTION_TRANSITIONS, optionally recording the vendor id and result."""
    old = execution.status
    execution.status = transition_execution(old, target).value
    if external_id is not None:
        execution.external_id = external_id
    if result is not None:
        execution.result = result
    execution.updated_at = at
    session.flush()
    return old


# ---- executions -----------------------------------------------------------------------------------
def get_execution_for_proposal(session: Session, proposal_id: str) -> m.Execution | None:
    return session.scalars(select(m.Execution).where(m.Execution.proposal_id == proposal_id)).one_or_none()


def get_execution(session: Session, execution_id: int, *, lock: bool = False) -> m.Execution | None:
    return session.get(m.Execution, execution_id, with_for_update=lock)


def insert_execution_if_new(
    session: Session, *, proposal_id: str, idempotency_key: str, vendor_request_id: str | None
) -> bool:
    """Store the key BEFORE the vendor call. True if this call created the row, False if one already exists."""
    stmt = (
        pg_insert(m.Execution)
        .values(
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            vendor_request_id=vendor_request_id,
            status="PENDING",
        )
        .on_conflict_do_nothing()
        .returning(m.Execution.id)
    )
    return session.execute(stmt).scalar_one_or_none() is not None


def find_execution_for_event(
    session: Session, *, request_id: str | None, resource_id: str | None
) -> m.Execution | None:
    """The execution a webhook is about: by the request id we sent first, else by the vendor's own id."""
    if request_id:
        row = session.scalars(select(m.Execution).where(m.Execution.vendor_request_id == request_id)).first()
        if row is not None:
            return row
    if resource_id:
        return session.scalars(select(m.Execution).where(m.Execution.external_id == resource_id)).first()
    return None


def list_executions(session: Session, status: str, *, limit: int = 100) -> Sequence[m.Execution]:
    stmt = select(m.Execution).where(m.Execution.status == status).order_by(m.Execution.id).limit(limit)
    return session.scalars(stmt).all()


def list_proposals_with_status(session: Session, status: str, *, limit: int = 100) -> Sequence[m.Proposal]:
    stmt = select(m.Proposal).where(m.Proposal.status == status).order_by(m.Proposal.proposed_at, m.Proposal.id)
    return session.scalars(stmt.limit(limit)).all()


# ---- webhook events -------------------------------------------------------------------------------
def insert_webhook_event_if_new(
    session: Session,
    *,
    event_id: str,
    name: str,
    resource_id: str | None,
    request_id: str | None,
    event_created_at: datetime | None,
) -> bool:
    """True if this event id is new. The primary key is the deduplication: a second delivery inserts nothing."""
    stmt = (
        pg_insert(m.WebhookEvent)
        .values(
            id=event_id,
            name=name,
            resource_id=resource_id,
            request_id=request_id,
            event_created_at=event_created_at,
            outcome="unmatched",
        )
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(m.WebhookEvent.id)
    )
    return session.execute(stmt).scalar_one_or_none() is not None


def get_webhook_event(session: Session, event_id: str) -> m.WebhookEvent | None:
    return session.get(m.WebhookEvent, event_id)


def list_unmatched_events(session: Session, *, since: datetime, limit: int = 200) -> Sequence[m.WebhookEvent]:
    """Events still waiting for an execution, received at or after `since` (older ones are given up on)."""
    stmt = select(m.WebhookEvent).where(m.WebhookEvent.outcome == "unmatched", m.WebhookEvent.received_at >= since)
    return session.scalars(stmt.order_by(m.WebhookEvent.received_at).limit(limit)).all()


# ---- approvals and decisions used by the execution gate ------------------------------------------
def approval_for(session: Session, proposal_id: str) -> m.Approval | None:
    return session.scalars(select(m.Approval).where(m.Approval.proposal_id == proposal_id)).one_or_none()


def escalations_before(session: Session, proposal_id: str, before: datetime) -> Sequence[m.PolicyDecision]:
    """ESCALATE decisions made at or before `before`: what a human approval at that time could have covered."""
    stmt = (
        select(m.PolicyDecision)
        .where(
            m.PolicyDecision.proposal_id == proposal_id,
            m.PolicyDecision.verdict == "ESCALATE",
            m.PolicyDecision.decided_at <= before,
        )
        .order_by(m.PolicyDecision.id)
    )
    return session.scalars(stmt).all()


def list_business_ids(session: Session) -> Sequence[str]:
    return session.scalars(select(m.Business.id).order_by(m.Business.id)).all()


def lock_execution(session: Session, execution_id: int) -> m.Execution | None:
    """The execution row, locked for this transaction, refreshed from the database (never a cached copy)."""
    stmt = (
        select(m.Execution)
        .where(m.Execution.id == execution_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.scalars(stmt).one_or_none()

"""Apply an execution's outcome: the one place that moves an execution and its proposal together and audits it.

Used by the worker (after a vendor call) and by the webhook endpoint (after a vendor event). Every move goes through
the lifecycle tables, so an event that would move a state backwards raises IllegalTransitionError; callers treat that
as "already past this point" and ignore the event.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session
from tessera_airwallex._status import transfer_status_for_event
from tessera_audit import AuditEventType, AuditLog
from tessera_audit import payloads as audit_payloads
from tessera_core.domain import IllegalTransitionError
from tessera_store import models as m
from tessera_store import repo_exec

from tessera_worker.dispatch import Dispatched


def _proposal_of(session: Session, execution: m.Execution) -> m.Proposal:
    proposal = session.get(m.Proposal, execution.proposal_id, with_for_update=True)
    if proposal is None:  # the foreign key makes this impossible; fail loudly rather than skip silently
        raise LookupError(f"execution {execution.id} has no proposal")
    return proposal


def record_submission(
    session: Session, audit: AuditLog, execution: m.Execution, done: Dispatched, *, at: datetime
) -> None:
    """After the call returned: PENDING -> SUBMITTED, or straight to SETTLED/FAILED if that is already the answer."""
    proposal = _proposal_of(session, execution)
    if done.state == "FAILED":
        _finish(session, audit, execution, proposal, "FAILED", "adapter", done.detail, at)
        return
    merged = {**(execution.result or {}), **done.detail}
    repo_exec.move_execution(session, execution, "SUBMITTED", external_id=done.external_id, result=merged, at=at)
    repo_exec.move_proposal(session, proposal, "SUBMITTED")
    audit.append(
        AuditEventType.EXECUTION_SUBMITTED,
        audit_payloads.execution_submitted_payload(
            proposal.id,
            execution_id=execution.id,
            idempotency_key=execution.idempotency_key,
            action_type=proposal.action_type,
            external_id=done.external_id,
        ),
    )
    if done.state == "SETTLED":
        _finish(session, audit, execution, proposal, "SETTLED", "adapter", done.detail, at)


def record_vendor_outcome(
    session: Session,
    audit: AuditLog,
    execution: m.Execution,
    target: str,
    *,
    source: str,
    detail: dict[str, Any] | None,
    at: datetime,
) -> None:
    """SUBMITTED -> SETTLED or FAILED, from a webhook. Raises IllegalTransitionError if it is too late or too early."""
    proposal = _proposal_of(session, execution)
    _finish(session, audit, execution, proposal, target, source, detail, at)


def _finish(
    session: Session,
    audit: AuditLog,
    execution: m.Execution,
    proposal: m.Proposal,
    target: str,
    source: str,
    detail: dict[str, Any] | None,
    at: datetime,
) -> None:
    result = {**(execution.result or {}), **(detail or {})}
    repo_exec.move_execution(session, execution, target, result=result, at=at)
    repo_exec.move_proposal(session, proposal, target)
    audit.append(
        AuditEventType.EXECUTION_RESULT,
        audit_payloads.execution_result_payload(
            proposal.id, execution_id=execution.id, outcome=target, source=source, detail=detail
        ),
    )


def apply_event(
    session: Session, audit: AuditLog, *, name: str, resource_id: str | None, request_id: str | None, at: datetime
) -> tuple[str, int | None]:
    """What a webhook does to the execution it is about. Returns (webhook outcome, execution id).

    applied    the execution (and its proposal) moved to SETTLED or FAILED
    ignored    nothing to do: not a final transfer event, or the execution is already past it (events can arrive
               duplicated and out of order, so a late event never moves a state backwards)
    unmatched  no execution knows this event yet, or the execution is still PENDING (its submission is not recorded
               yet); the worker tries again when it reconciles
    """
    target = transfer_status_for_event(name)
    if target is None:
        return "ignored", None
    execution = repo_exec.find_execution_for_event(session, request_id=request_id, resource_id=resource_id)
    if execution is None:
        return "unmatched", None
    if execution.status == "PENDING":
        return "unmatched", execution.id
    try:
        with session.begin_nested():  # a refused move must leave neither the execution nor the proposal half-moved
            record_vendor_outcome(
                session, audit, execution, target.value, source="webhook", detail={"event": name}, at=at
            )
    except IllegalTransitionError:
        return "ignored", execution.id
    return "applied", execution.id

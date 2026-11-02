"""Proposal listing and the human decision.

Approving marks a proposal APPROVED and nothing more. It does not call Airwallex, run policy, or create an
execution: the worker re-runs policy with fresh state at execution time, because the world may have changed since the
proposal was made. Only a PENDING_APPROVAL proposal can be decided, once; BLOCKED proposals have no approve path.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session
from tessera_audit import AuditEventType, AuditLog
from tessera_audit import payloads as audit_payloads
from tessera_store import repo

from tessera_api.deps import ACTOR, AppContext, get_ctx, get_session, require_api_key
from tessera_api.schemas import IDENT_PATTERN, ApprovalIn, Ident, ProposalOut, ProposalStatus
from tessera_api.views import proposal_outs

router = APIRouter(prefix="/proposals", tags=["proposals"], dependencies=[Depends(require_api_key)])

PendingId = Annotated[str, Path(min_length=1, max_length=128, pattern=IDENT_PATTERN)]


@router.get("", response_model=list[ProposalOut])
def list_proposals(
    business_id: Ident | None = None,
    status: ProposalStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> list[ProposalOut]:
    rows = repo.list_proposals(session, business_id=business_id, status=status, limit=limit, offset=offset)
    return proposal_outs(session, rows)


def _decide(
    proposal_id: str, *, approve: bool, body: ApprovalIn | None, ctx: AppContext, session: Session
) -> ProposalOut:
    proposal = repo.get_proposal(session, proposal_id, lock=True)  # row lock: two callers cannot both decide
    if proposal is None:
        raise HTTPException(status_code=404, detail="unknown proposal")
    if proposal.status != "PENDING_APPROVAL":
        raise HTTPException(status_code=409, detail=f"proposal is {proposal.status}, not PENDING_APPROVAL")
    note = body.note if body else None
    from_status = proposal.status
    repo.record_human_decision(
        session,
        proposal,
        decision="APPROVED" if approve else "REJECTED",
        actor=ACTOR,
        note=note,
        new_status="APPROVED" if approve else "REJECTED",
    )
    AuditLog(session, ctx.clock).append(
        AuditEventType.APPROVED if approve else AuditEventType.REJECTED,
        audit_payloads.human_decision_payload(proposal_id, actor=ACTOR, note=note, from_status=from_status),
    )
    session.commit()
    return proposal_outs(session, [proposal])[0]


@router.post("/{proposal_id}/approve", response_model=ProposalOut)
def approve(
    proposal_id: PendingId,
    body: ApprovalIn | None = None,
    ctx: AppContext = Depends(get_ctx),
    session: Session = Depends(get_session),
) -> ProposalOut:
    return _decide(proposal_id, approve=True, body=body, ctx=ctx, session=session)


@router.post("/{proposal_id}/reject", response_model=ProposalOut)
def reject(
    proposal_id: PendingId,
    body: ApprovalIn | None = None,
    ctx: AppContext = Depends(get_ctx),
    session: Session = Depends(get_session),
) -> ProposalOut:
    return _decide(proposal_id, approve=False, body=body, ctx=ctx, session=session)

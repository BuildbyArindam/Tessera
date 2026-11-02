from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from tessera_audit import AuditLog

from tessera_api.deps import AppContext, get_ctx, get_session, require_api_key
from tessera_api.schemas import AuditPage, AuditRecordOut, ChainStatusOut

router = APIRouter(prefix="/audit", tags=["audit"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=AuditPage)
def list_audit(
    after_seq: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    ctx: AppContext = Depends(get_ctx),
    session: Session = Depends(get_session),
) -> AuditPage:
    """Events in order, `limit` at a time. Pass `next_after_seq` as `after_seq` to read on."""
    records = AuditLog(session, ctx.clock).read(after_seq=after_seq, limit=limit)
    events = [
        AuditRecordOut(
            seq=r.seq,
            timestamp=r.timestamp,
            event_type=r.event_type,
            payload=r.payload,
            prev_hash=r.prev_hash,
            hash=r.hash,
        )
        for r in records
    ]
    return AuditPage(events=events, next_after_seq=events[-1].seq if len(events) == limit else None)


@router.get("/verify", response_model=ChainStatusOut)
def verify_audit(ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)) -> ChainStatusOut:
    """Re-walk the whole chain. `ok` is true, or `broken_seq` names the first row that fails."""
    status = AuditLog(session, ctx.clock).verify_chain()
    return ChainStatusOut(
        ok=status.ok,
        checked=status.checked,
        head_seq=status.head_seq,
        head_hash=status.head_hash,
        broken_seq=status.broken_seq,
        reason=status.reason,
    )

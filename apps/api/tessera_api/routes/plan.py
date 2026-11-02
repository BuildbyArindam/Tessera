from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from tessera_agent import LLMError
from tessera_audit import AuditLog
from tessera_store import models as m
from tessera_store import repo

from tessera_api.deps import AppContext, get_ctx, get_session, require_api_key
from tessera_api.planning import PlanningUnavailable, run_plan
from tessera_api.schemas import PlanRunIn, PlanRunOut
from tessera_api.views import proposal_outs

router = APIRouter(prefix="/plan", tags=["plan"], dependencies=[Depends(require_api_key)])


@router.post("/run", response_model=PlanRunOut)
def plan_run(
    body: PlanRunIn, ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)
) -> PlanRunOut:
    """Run the planner for one business and put every proposal through the policy engine.

    Nothing is executed. APPROVE -> APPROVED, ESCALATE -> PENDING_APPROVAL, BLOCK -> BLOCKED. Repeating the call on
    an unchanged state creates nothing new.
    """
    if ctx.world_provider is None:
        raise HTTPException(status_code=503, detail="planning is not configured: no data provider is attached")
    if repo.get_business(session, body.business_id) is None:
        raise HTTPException(status_code=404, detail="unknown business_id")
    try:
        world = ctx.world_provider.load(session, body.business_id)
    except PlanningUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    if world.snapshot.business_id != body.business_id:
        raise HTTPException(status_code=500, detail="the data provider returned another business")
    try:
        llm = ctx.llm_factory()
    except LLMError:
        raise HTTPException(status_code=503, detail="the language model is not configured") from None

    session.commit()  # end the read transaction: the model call below can take seconds
    summary = run_plan(
        session,
        world=world,
        llm=llm,
        ruleset=ctx.ruleset,
        audit=AuditLog(session, ctx.clock),
        now=ctx.clock(),
    )
    session.commit()

    rows = list(session.scalars(select(m.Proposal).where(m.Proposal.id.in_(summary.created)).order_by(m.Proposal.id)))
    return PlanRunOut(
        outcome=summary.outcome,
        model=summary.model,
        prompt_hash=summary.prompt_hash,
        input_flags=list(summary.input_flags),
        created=proposal_outs(session, rows),
        already_known=list(summary.already_known),
    )

"""The autonomy dial: read the level, and take a person's decisions about it.

GET reads and, if an evidence source is configured, shows what the controller would do now (it applies nothing). POST
takes one of three actions: confirm a pending promotion, reject it, or lower the level. There is no action that RAISES
the level directly: a promotion is proposed by the controller from evidence, and confirming it re-checks that evidence,
so a person cannot grant more authority than the evidence supports. Lowering is always allowed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from tessera_audit import AuditLog
from tessera_core.autonomy import Assessment, AutonomyLevel
from tessera_store import models as m
from tessera_store import repo_autonomy

from tessera_api import autonomy_service as service
from tessera_api.deps import ACTOR, AppContext, get_ctx, get_session, require_api_key
from tessera_api.schemas import (
    AssessmentOut,
    AutonomyAction,
    AutonomyOut,
    ConfirmPromotionIn,
    CriterionOut,
    DemoteIn,
    LevelName,
    PromotionOut,
    RejectPromotionIn,
)

log = logging.getLogger("tessera.api.autonomy")
router = APIRouter(prefix="/autonomy", tags=["autonomy"], dependencies=[Depends(require_api_key)])


_NAMES: dict[AutonomyLevel, LevelName] = {
    AutonomyLevel.L0: "L0",
    AutonomyLevel.L1: "L1",
    AutonomyLevel.L2: "L2",
    AutonomyLevel.L3: "L3",
}


def _name(level: int) -> LevelName:
    return _NAMES[AutonomyLevel(level)]


def promotion_out(row: m.AutonomyPromotion) -> PromotionOut:
    return PromotionOut(
        id=row.id,
        from_level=_name(row.from_level),
        to_level=_name(row.to_level),
        status=row.status,
        proposed_at=row.proposed_at,
        decided_at=row.decided_at,
        actor=row.actor,
        note=row.note,
    )


def assessment_out(a: Assessment) -> AssessmentOut:
    return AssessmentOut(
        kind=a.kind.value,
        from_level=_name(a.from_level),
        to_level=_name(a.to_level),
        triggers=list(a.triggers),
        criteria=[
            CriterionOut(name=c.name, passed=c.passed, observed=c.observed, required=c.required) for c in a.criteria
        ],
        eligible=a.eligible,
        config_hash=a.config_hash,
        assessed_at=a.assessed_at,
    )


def _current(ctx: AppContext, session: Session) -> AutonomyOut:
    row = repo_autonomy.get_state_row(session)
    pending = repo_autonomy.get_pending_promotion(session)
    assessment: AssessmentOut | None = None
    if ctx.autonomy_inputs is not None:
        try:
            now = ctx.clock()
            inputs = ctx.autonomy_inputs.load(session, now)
            assessment = assessment_out(
                ctx.autonomy_controller.assess(
                    repo_autonomy.to_state(row), inputs.evidence, now, kill_switch_engaged=inputs.kill_switch_engaged
                )
            )
        except Exception:  # a broken evidence source must not take the level read down with it
            log.exception("autonomy evidence could not be loaded")
    return AutonomyOut(
        level=_name(row.level),
        since=row.since,
        pending_promotion=None if pending is None else promotion_out(pending),
        evidence_available=assessment is not None,
        assessment=assessment,
    )


@router.get("", response_model=AutonomyOut)
def get_autonomy(ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)) -> AutonomyOut:
    result = _current(ctx, session)
    session.commit()  # get_state_row may have created the row on a database that never had one
    return result


@router.post("", response_model=AutonomyOut)
def post_autonomy(
    action: AutonomyAction, ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)
) -> AutonomyOut:
    audit, now = AuditLog(session, ctx.clock), ctx.clock()
    if isinstance(action, ConfirmPromotionIn):
        outcome = service.confirm_promotion(
            session,
            ctx.autonomy_controller,
            ctx.autonomy_inputs,
            promotion_id=action.promotion_id,
            actor=ACTOR,
            note=action.note,
            now=now,
            audit=audit,
        )
    elif isinstance(action, RejectPromotionIn):
        outcome = service.reject_promotion(
            session, promotion_id=action.promotion_id, actor=ACTOR, note=action.note, now=now, audit=audit
        )
    else:
        assert isinstance(action, DemoteIn)
        outcome = service.demote_by_operator(
            session, to_level=AutonomyLevel[action.to_level], actor=ACTOR, note=action.note, now=now, audit=audit
        )
    session.commit()  # a refused confirmation can still have cancelled a promotion or demoted: that must persist
    if not outcome.ok:
        raise HTTPException(status_code=404 if outcome.label == "unknown" else 409, detail=outcome.detail)
    return _current(ctx, session)

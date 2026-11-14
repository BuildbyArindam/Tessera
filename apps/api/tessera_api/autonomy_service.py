"""Apply the autonomy controller's answers to the database, and take a person's decisions about the dial.

Shared by the worker (automatic demotion, proposing a promotion) and the API (confirm, reject, demote). Like the repo
functions, nothing here commits: the caller commits, so each level change lands in the same transaction as its audit
event. Every function that changes the level locks the state row first, so two writers cannot both move the dial.

The rules this file enforces:
* Demotion is automatic and immediate. Losing authority never waits for a person.
* A promotion is only ever PROPOSED here. It changes the level only when a person confirms it, and confirmation
  re-assesses the evidence first: what justified the proposal may no longer hold (time-of-check to time-of-use).
* A level change cancels any open promotion, which was justified by the level it started from.
* Where the controller's inputs are unavailable, nothing is promoted and nothing is confirmed. Fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session
from tessera_audit import AuditEventType, AuditLog
from tessera_audit import payloads as audit_payloads
from tessera_core.autonomy import Assessment, AutonomyController, AutonomyLevel, Evidence, TransitionKind
from tessera_store import models as m
from tessera_store import repo_autonomy

CONTROLLER_ACTOR = "controller"


@dataclass(frozen=True)
class AutonomyInputs:
    """What the controller judges: the evidence window, and whether the operator's kill switch is engaged."""

    evidence: Evidence
    kill_switch_engaged: bool


class AutonomyInputsProvider(Protocol):
    """Builds the evidence from what happened (violations, shadow comparisons, realised savings, coverage).

    Not implemented in the repo yet: realised saving and coverage need the Day 17 evaluation data. Tests inject one;
    until one is configured, nothing is promoted and a confirmation is refused.
    """

    def load(self, session: Session, now: datetime) -> AutonomyInputs: ...


@dataclass(frozen=True)
class Outcome:
    """What happened, as a short label for the worker's cycle report and the API's answer."""

    label: str
    ok: bool = True
    detail: str = ""


def _log(
    audit: AuditLog,
    kind: str,
    *,
    from_level: AutonomyLevel,
    to_level: AutonomyLevel,
    actor: str,
    assessment: Assessment | None = None,
    promotion_id: int | None = None,
    note: str | None = None,
) -> None:
    payload = audit_payloads.autonomy_changed_payload(
        kind=kind,
        from_level=from_level,
        to_level=to_level,
        actor=actor,
        triggers=() if assessment is None else assessment.triggers,
        promotion_id=promotion_id,
        assessment=assessment,
        note=note,
    )
    audit.append(AuditEventType.AUTONOMY_CHANGED, payload)


def _cancel_pending(session: Session, audit: AuditLog, *, at: datetime, actor: str, note: str) -> None:
    pending = repo_autonomy.get_pending_promotion(session, lock=True)
    if pending is not None:
        repo_autonomy.close_promotion(session, pending, "CANCELLED", at=at, actor=actor, note=note)
        _log(
            audit,
            "promotion_cancelled",
            from_level=AutonomyLevel(pending.from_level),
            to_level=AutonomyLevel(pending.to_level),
            actor=actor,
            promotion_id=pending.id,
            note=note,
        )


def _demote(
    session: Session,
    audit: AuditLog,
    row: m.AutonomyState,
    to_level: AutonomyLevel,
    *,
    at: datetime,
    actor: str,
    assessment: Assessment | None,
    note: str | None = None,
) -> None:
    old = repo_autonomy.set_level(session, row, to_level, at)
    _log(audit, "demoted", from_level=old, to_level=to_level, actor=actor, assessment=assessment, note=note)
    _cancel_pending(session, audit, at=at, actor=actor, note="the level changed")


# ---- the controller's automatic moves ------------------------------------------------------------
def evaluate_and_apply(
    session: Session,
    controller: AutonomyController,
    inputs: AutonomyInputs,
    *,
    now: datetime,
    audit: AuditLog,
) -> tuple[Assessment, Outcome]:
    """Assess, then apply what is automatic: a demotion happens now; a promotion becomes a pending proposal."""
    row = repo_autonomy.get_state_row(session, lock=True)
    assessment = controller.assess(
        repo_autonomy.to_state(row), inputs.evidence, now, kill_switch_engaged=inputs.kill_switch_engaged
    )
    if assessment.kind is TransitionKind.DEMOTE:
        _demote(session, audit, row, assessment.to_level, at=now, actor=CONTROLLER_ACTOR, assessment=assessment)
        return assessment, Outcome("demoted", detail=",".join(assessment.triggers))
    pending = repo_autonomy.get_pending_promotion(session, lock=True)
    if assessment.kind is TransitionKind.PROPOSE_PROMOTION:
        if pending is not None:
            return assessment, Outcome("promotion_already_pending")
        promotion = repo_autonomy.create_promotion(
            session,
            from_level=assessment.from_level,
            to_level=assessment.to_level,
            assessment=assessment.model_dump(mode="json"),
            at=now,
        )
        _log(
            audit,
            "promotion_proposed",
            from_level=assessment.from_level,
            to_level=assessment.to_level,
            actor=CONTROLLER_ACTOR,
            assessment=assessment,
            promotion_id=promotion.id,
        )
        return assessment, Outcome("promotion_proposed")
    if pending is not None and not assessment.eligible:  # the evidence that justified it has gone
        _cancel_pending(session, audit, at=now, actor=CONTROLLER_ACTOR, note="evidence changed")
        return assessment, Outcome("promotion_cancelled")
    return assessment, Outcome("no_change")


# ---- a person's decisions ------------------------------------------------------------------------
def confirm_promotion(
    session: Session,
    controller: AutonomyController,
    provider: AutonomyInputsProvider | None,
    *,
    promotion_id: int,
    actor: str,
    note: str | None,
    now: datetime,
    audit: AuditLog,
) -> Outcome:
    """Apply a promotion a person confirmed, if the evidence STILL supports it. Otherwise nothing is promoted."""
    row = repo_autonomy.get_state_row(session, lock=True)
    promotion = repo_autonomy.get_promotion(session, promotion_id, lock=True)
    if promotion is None:
        return Outcome("unknown", ok=False, detail="no such promotion")
    if promotion.status != "PENDING":
        return Outcome("not_pending", ok=False, detail=f"promotion is {promotion.status}, not PENDING")
    if promotion.from_level != row.level:
        _cancel_pending(session, audit, at=now, actor=actor, note="the level changed")
        return Outcome("stale", ok=False, detail="the level changed since this promotion was proposed; it is cancelled")
    if provider is None:
        return Outcome(
            "no_evidence_source", ok=False, detail="evidence cannot be re-checked here, so nothing is promoted"
        )

    inputs = provider.load(session, now)
    fresh = controller.assess(
        repo_autonomy.to_state(row), inputs.evidence, now, kill_switch_engaged=inputs.kill_switch_engaged
    )
    if fresh.kind is TransitionKind.DEMOTE:
        _demote(session, audit, row, fresh.to_level, at=now, actor=CONTROLLER_ACTOR, assessment=fresh)
        return Outcome("demoted", ok=False, detail=f"demoted instead: {','.join(fresh.triggers)}")
    if fresh.kind is not TransitionKind.PROPOSE_PROMOTION or int(fresh.to_level) != promotion.to_level:
        failing = ",".join(c.name for c in fresh.criteria if not c.passed) or "level ceiling"
        _cancel_pending(session, audit, at=now, actor=actor, note=f"no longer supported: {failing}")
        return Outcome("no_longer_supported", ok=False, detail=f"the evidence no longer supports it: {failing}")

    old = repo_autonomy.set_level(session, row, fresh.to_level, now)
    repo_autonomy.close_promotion(session, promotion, "CONFIRMED", at=now, actor=actor, note=note)
    _log(
        audit,
        "promotion_confirmed",
        from_level=old,
        to_level=fresh.to_level,
        actor=actor,
        assessment=fresh,
        promotion_id=promotion.id,
        note=note,
    )
    return Outcome("promoted")


def reject_promotion(
    session: Session, *, promotion_id: int, actor: str, note: str | None, now: datetime, audit: AuditLog
) -> Outcome:
    repo_autonomy.get_state_row(session, lock=True)  # the same lock order as every level change: state, then promotion
    promotion = repo_autonomy.get_promotion(session, promotion_id, lock=True)
    if promotion is None:
        return Outcome("unknown", ok=False, detail="no such promotion")
    if promotion.status != "PENDING":
        return Outcome("not_pending", ok=False, detail=f"promotion is {promotion.status}, not PENDING")
    repo_autonomy.close_promotion(session, promotion, "REJECTED", at=now, actor=actor, note=note)
    _log(
        audit,
        "promotion_rejected",
        from_level=AutonomyLevel(promotion.from_level),
        to_level=AutonomyLevel(promotion.to_level),
        actor=actor,
        promotion_id=promotion.id,
        note=note,
    )
    return Outcome("rejected")


def demote_by_operator(
    session: Session, *, to_level: AutonomyLevel, actor: str, note: str | None, now: datetime, audit: AuditLog
) -> Outcome:
    """A person lowers the dial. Always allowed: a person can only ever make Tessera safer here, never less safe."""
    row = repo_autonomy.get_state_row(session, lock=True)
    if to_level >= AutonomyLevel(row.level):
        return Outcome("not_lower", ok=False, detail="the new level must be lower than the current one")
    _demote(session, audit, row, to_level, at=now, actor=actor, assessment=None, note=note)
    return Outcome("demoted")

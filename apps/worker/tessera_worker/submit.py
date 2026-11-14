"""Execute one APPROVED proposal: fresh policy, store the key, call the port, record the outcome.

The order is the safety argument. Each step is its own short transaction, and nothing is held open across the call:

 1. Lock the proposal; run policy AGAIN with fresh context (phase "execution"). The approval, by policy or by a
    human, described an earlier world. A BLOCK stops it, an uncovered ESCALATE sends it back to a human (gate.py).
 2. Insert the execution row with its idempotency key and commit. Only now may anything be sent.
 3. Call the port with that key. A crash anywhere after step 2 leaves a PENDING row; the next cycle repeats step 1 and
    replays the SAME key, so the vendor sees one request however often this runs.
 4. Lock the execution row and record the outcome (outcomes.py). If another process already did, do nothing.

A PENDING execution whose fresh check no longer passes is NOT cancelled, because the first call may have gone through
and a false FAILED would hide real money movement. It is held for a person, and so is one older than `max_age`
(the vendor's duplicate window is finite). Nothing is sent in either case.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker
from tessera_airwallex import AirwallexPort
from tessera_airwallex.sandbox import request_id_for
from tessera_api.planning import WorldProvider, decide_proposal
from tessera_audit import AuditEventType, AuditLog
from tessera_audit import payloads as audit_payloads
from tessera_core.domain import Proposal as ProposalModel
from tessera_core.domain import Verdict
from tessera_core.policy import Ruleset
from tessera_store import models as m
from tessera_store import repo, repo_autonomy, repo_exec

from tessera_worker.dispatch import Retry, VendorDirectory, dispatch
from tessera_worker.gate import Covered, effective_verdict
from tessera_worker.outcomes import record_submission

log = logging.getLogger("tessera.worker.submit")


@dataclass(frozen=True)
class ExecDeps:
    session_factory: sessionmaker[Session]
    port: AirwallexPort
    directory: VendorDirectory
    world_provider: WorldProvider | None
    ruleset: Ruleset
    clock: Callable[[], datetime]
    max_age: timedelta


def execution_key(proposal_id: str) -> str:
    """The idempotency key for a proposal: deterministic, so a restart derives the same one. 44 plain characters."""
    return "tsr-" + hashlib.sha256(f"execute:{proposal_id}".encode()).hexdigest()[:40]


def flag_codes_of(plan_record: dict[str, Any] | None) -> tuple[str, ...]:
    flags = (plan_record or {}).get("input_flags") or ()
    return tuple(sorted({str(f["code"]) for f in flags if isinstance(f, dict) and "code" in f}))


def _domain(row: m.Proposal) -> ProposalModel:
    return ProposalModel(
        id=row.id,
        action_type=row.action_type,
        params=row.params,
        rationale=row.rationale,
        confidence=row.confidence,
        created_at=row.proposed_at,
    )


def _covered(session: Session, proposal_id: str) -> Covered | None:
    approval = repo_exec.approval_for(session, proposal_id)
    if approval is None or approval.decision != "APPROVED":
        return None
    seen = [d for d in repo_exec.escalations_before(session, proposal_id, approval.decided_at) if d.ruleset_hash]
    if not seen:
        return None
    latest = seen[-1].ruleset_hash
    rules = frozenset(r for d in seen if d.ruleset_hash == latest for r in d.rule_ids)
    return Covered(rules, latest)


def process_proposal(deps: ExecDeps, proposal_id: str) -> str:
    """Run the whole sequence for one proposal. Returns a short label for the cycle report."""
    gate = _gate(deps, proposal_id)
    return gate if isinstance(gate, str) else run_execution(deps, gate)


def _gate(deps: ExecDeps, proposal_id: str) -> str | int:
    """Step 1 and 2. An execution id means "go"; a string is the reason it did not."""
    if deps.world_provider is None:
        return "deferred_no_world"
    audit_now = deps.clock()
    with deps.session_factory() as s:
        row = repo.get_proposal(s, proposal_id, lock=True)
        if row is None or row.status != "APPROVED":
            return "skipped"
        existing = repo_exec.get_execution_for_proposal(s, proposal_id)
        if existing is not None and audit_now - existing.created_at > deps.max_age:
            log.error("execution %s is PENDING past the replay window: manual review", existing.id)
            return "held_stale"
        try:
            world = deps.world_provider.load(s, row.business_id)
        except Exception:  # fail closed: no fresh world, no execution; the proposal stays APPROVED for the next cycle
            log.exception("fresh context unavailable for %s", proposal_id)
            return "deferred_no_world"
        proposal = _domain(row)
        fresh, context = decide_proposal(
            world, proposal, flag_codes_of(row.plan_record), deps.ruleset,
            autonomy_level=repo_autonomy.get_level(s),  # a demotion since approval is seen here and stops the action
        )  # fmt: skip
        log_audit = AuditLog(s, deps.clock)
        repo.add_decision(
            s,
            proposal_id=proposal_id,
            phase="execution",
            verdict=fresh.verdict.value,
            reasons=fresh.reasons,
            rule_ids=fresh.rule_ids,
            ruleset_hash=fresh.ruleset_hash,
            context=None if context is None else context.model_dump(mode="json"),
        )
        log_audit.append(
            AuditEventType.POLICY_DECISION,
            audit_payloads.decision_payload(proposal_id, fresh, context, phase="execution"),
        )
        verdict = effective_verdict(fresh, _covered(s, proposal_id))
        if verdict is not Verdict.APPROVE:
            return _stop(s, log_audit, row, existing, verdict, fresh)
        if existing is None:
            key = execution_key(proposal_id)
            repo_exec.insert_execution_if_new(
                s, proposal_id=proposal_id, idempotency_key=key, vendor_request_id=request_id_for(key)
            )
        s.commit()  # the key is durable before anything is sent
        stored = repo_exec.get_execution_for_proposal(s, proposal_id)
        assert stored is not None
        return stored.id


def _stop(
    s: Session, log_audit: AuditLog, row: m.Proposal, existing: m.Execution | None, verdict: Verdict, fresh: Any
) -> str:
    if existing is not None:  # a request may already be out there: hold it, never relabel it
        s.commit()
        return "held_for_review"
    if verdict is Verdict.BLOCK:
        repo_exec.move_proposal(s, row, "BLOCKED")
        s.commit()
        return "blocked"
    repo_exec.move_proposal(s, row, "PENDING_APPROVAL")
    log_audit.append(AuditEventType.APPROVAL_REQUESTED, audit_payloads.approval_requested_payload(row.id, fresh))
    s.commit()
    return "re_escalated"


def run_execution(deps: ExecDeps, execution_id: int) -> str:
    """Steps 3 and 4: call the port with the stored key, then record what happened."""
    with deps.session_factory() as s:
        ex = repo_exec.get_execution(s, execution_id)
        prop = s.get(m.Proposal, ex.proposal_id) if ex is not None else None
        if ex is None or prop is None or ex.status != "PENDING":
            return "already_done"
        action, params, business = prop.action_type, dict(prop.params), prop.business_id
        key, prior = ex.idempotency_key, dict(ex.result or {})

    def save(extra: dict[str, Any]) -> None:
        with deps.session_factory() as inner:
            locked = repo_exec.lock_execution(inner, execution_id)
            if locked is not None:
                locked.result = {**(locked.result or {}), **extra}
                inner.commit()

    try:
        done = dispatch(
            deps.port, action_type=action, params=params, business_id=business, key=key,
            directory=deps.directory, prior=prior, save=save,
        )  # fmt: skip
    except Retry as exc:
        log.warning("execution %s will be replayed with the same key (%s)", execution_id, exc)
        return "retry_later"
    with deps.session_factory() as s:
        ex = repo_exec.lock_execution(s, execution_id)
        if ex is None or ex.status != "PENDING":
            return "already_done"
        record_submission(s, AuditLog(s, deps.clock), ex, done, at=deps.clock())
        s.commit()
    return done.state.lower()

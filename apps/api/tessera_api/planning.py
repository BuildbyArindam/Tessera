"""One planning run: planner -> policy -> storage -> audit, for one business.

The planner proposes; it holds no authority. Every proposal goes to the deterministic policy engine, and the verdict
decides where it lands: APPROVE -> APPROVED (ready for the worker), ESCALATE -> PENDING_APPROVAL (a human decides),
BLOCK -> BLOCKED (terminal). Nothing here executes anything or imports an Airwallex module.

The inputs a run needs (balances, FX history, beneficiaries, obligations, the policy context) come from a
`WorldProvider`. This API does not assemble them itself: the worker owns data ingest (Day 13), and balances come
from Airwallex, which only the worker may call. Until a provider is configured, `POST /plan/run` answers 503.

A run is safe to repeat. The planner derives proposal ids from the state it saw, so planning an unchanged state
again finds the same ids, writes nothing new, and reports them as already known.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session
from tessera_agent import LLMClient, PlannerSnapshot
from tessera_agent import Planner as AgentPlanner
from tessera_audit import AuditEventType, AuditLog
from tessera_audit import payloads as audit_payloads
from tessera_core.domain import PolicyDecision, Proposal, Verdict
from tessera_core.policy import AutonomyLevel, PolicyContext, Ruleset, evaluate
from tessera_store import repo, repo_autonomy

log = logging.getLogger("tessera.api.planning")

STATUS_FOR_VERDICT = {
    Verdict.APPROVE: "APPROVED",
    Verdict.ESCALATE: "PENDING_APPROVAL",
    Verdict.BLOCK: "BLOCKED",
}


class PlanningUnavailable(RuntimeError):
    """The inputs for a plan run cannot be assembled right now. The message is shown to the caller: keep it generic."""


@dataclass(frozen=True)
class PlanningWorld:
    snapshot: PlannerSnapshot
    # (proposal, input-screening flag codes from the planner's record) -> the context policy judges it in.
    policy_context: Callable[[Proposal, tuple[str, ...]], PolicyContext]


class WorldProvider(Protocol):
    def load(self, session: Session, business_id: str) -> PlanningWorld: ...


@dataclass(frozen=True)
class PlanRunSummary:
    outcome: str
    model: str
    prompt_hash: str
    input_flags: tuple[str, ...]
    created: tuple[str, ...]
    already_known: tuple[str, ...]


def decide_proposal(
    world: PlanningWorld,
    proposal: Proposal,
    flag_codes: tuple[str, ...],
    ruleset: Ruleset,
    *,
    autonomy_level: AutonomyLevel | None = None,
) -> tuple[PolicyDecision, PolicyContext | None]:
    """Policy's verdict for one proposal. If the context cannot be built, the answer is BLOCK, never APPROVE.

    `autonomy_level` is the stored level (tessera_store.repo_autonomy). When given it REPLACES whatever level the world
    provider put in the context, so the dial has one authority and a provider cannot claim more than was earned. Both
    production callers (a planning run, and the worker's execution-time re-check) always pass it.
    """
    try:
        context = world.policy_context(proposal, flag_codes)
        if autonomy_level is not None:
            context = context.model_copy(update={"autonomy_level": autonomy_level})
    except Exception:  # fail closed: whatever went wrong, nothing may be approved on the strength of it
        log.exception("policy context could not be built for %s", proposal.id)
        failed = PolicyDecision(
            verdict=Verdict.BLOCK,
            reasons=("policy_error", "engine: policy context could not be built"),
            rule_ids=("engine",),
            ruleset_hash=ruleset.hash,
        )
        return failed, None
    return evaluate(proposal, context, ruleset), context


def run_plan(
    session: Session,
    *,
    world: PlanningWorld,
    llm: LLMClient,
    ruleset: Ruleset,
    audit: AuditLog,
    now: datetime,
) -> PlanRunSummary:
    business_id = world.snapshot.business_id
    result = AgentPlanner(llm).run(world.snapshot, now=now)  # the slow, networked step: no lock is held yet
    level = repo_autonomy.get_level(session)  # read after the slow step, so it is as fresh as this run can make it
    record = result.record
    plan_dump = record.model_dump(mode="json")
    created: list[str] = []
    known: list[str] = []
    for proposal in result.proposals:
        if repo.get_proposal(session, proposal.id) is not None:
            known.append(proposal.id)
            continue
        decision, context = decide_proposal(world, proposal, result.flag_codes, ruleset, autonomy_level=level)
        inserted = repo.insert_proposal_if_new(
            session,
            proposal,
            business_id=business_id,
            status=STATUS_FOR_VERDICT[decision.verdict],
            plan_record=plan_dump,
        )
        if not inserted:  # another run wrote the same id between our check and our insert
            known.append(proposal.id)
            continue
        audit.append(AuditEventType.PROPOSAL_CREATED, audit_payloads.proposal_payload(proposal, business_id, plan_dump))
        repo.add_decision(
            session,
            proposal_id=proposal.id,
            phase="plan",
            verdict=decision.verdict.value,
            reasons=decision.reasons,
            rule_ids=decision.rule_ids,
            ruleset_hash=decision.ruleset_hash,
            context=None if context is None else context.model_dump(mode="json"),
        )
        audit.append(AuditEventType.POLICY_DECISION, audit_payloads.decision_payload(proposal.id, decision, context))
        if decision.verdict is Verdict.ESCALATE:
            audit.append(
                AuditEventType.APPROVAL_REQUESTED, audit_payloads.approval_requested_payload(proposal.id, decision)
            )
        created.append(proposal.id)
    return PlanRunSummary(
        outcome=record.outcome,
        model=record.model,
        prompt_hash=record.prompt_hash,
        input_flags=result.flag_codes,
        created=tuple(created),
        already_known=tuple(known),
    )

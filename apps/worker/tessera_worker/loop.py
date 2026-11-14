"""The worker cycle: ingest, forecast, plan, execute, reconcile. Safe to run twice and safe to kill at any point.

  0. autonomy    first of all: assess the evidence; demote at once on a trigger, else maybe propose a promotion
  a. ingest      an injected hook per business (data arrives from Airwallex and the books; see TODO in __main__)
  b. forecast    an injected hook returning ForecastResults; new ones are stored and audited
  c. plan        the planner proposes; policy judges each proposal; the verdict decides where it lands:
  d/e.           APPROVE -> APPROVED, ESCALATE -> PENDING_APPROVAL (queued for a human), BLOCK -> BLOCKED (logged)
  f. execute     every APPROVED proposal goes through submit.process_proposal: fresh policy, key stored, then the port
  g. reconcile   webhook events that arrived before their execution was recorded are applied now

Why a rerun is harmless: proposal ids are derived from the state the planner saw, so replanning an unchanged state
writes nothing; forecasts are unique per business, currency, horizon, date and model; the execution key is derived
from the proposal id and stored before the call; webhook events are deduplicated by id. A failure for one business is
logged and counted, and the others still run.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session
from tessera_agent import LLMClient
from tessera_api import autonomy_service
from tessera_api.planning import run_plan
from tessera_audit import AuditEventType, AuditLog
from tessera_audit import payloads as audit_payloads
from tessera_core.autonomy import AutonomyController
from tessera_core.forecast import ForecastResult
from tessera_store import repo, repo_exec

from tessera_worker.outcomes import apply_event
from tessera_worker.submit import ExecDeps, process_proposal

log = logging.getLogger("tessera.worker.loop")

IngestStep = Callable[[Session, str], None]
ForecastStep = Callable[[Session, str, date], Sequence[ForecastResult]]
EXECUTE_BATCH = 50


@dataclass(frozen=True)
class AutonomyDeps:
    controller: AutonomyController
    inputs: autonomy_service.AutonomyInputsProvider


@dataclass(frozen=True)
class LoopDeps:
    exec: ExecDeps
    llm_factory: Callable[[], LLMClient]
    ingest: IngestStep | None = None
    forecast: ForecastStep | None = None
    reconcile_window: timedelta = timedelta(days=7)
    # None: the dial does not move on its own (it stays where it is; a person can still lower it through the API).
    autonomy: AutonomyDeps | None = None


def run_cycle(deps: LoopDeps) -> Counter[str]:
    """One pass over everything. Returns counters for the log; never raises for a single business or proposal."""
    report: Counter[str] = Counter()
    _autonomy_step(deps, report)  # before anything is planned or executed, so a demotion applies to this very cycle
    with deps.exec.session_factory() as session:
        businesses = list(repo_exec.list_business_ids(session))
    for business_id in businesses:
        try:
            _business_cycle(deps, business_id, report)
        except Exception:
            log.exception("cycle failed for business %s", business_id)
            report["business_errors"] += 1
    _execute_approved(deps, report)
    _reconcile(deps, report)
    return report


def _autonomy_step(deps: LoopDeps, report: Counter[str]) -> None:
    """Assess the evidence and apply what is automatic. A failure leaves the level where it is and is counted."""
    if deps.autonomy is None:
        report["autonomy_skipped_no_inputs"] += 1
        return
    try:
        with deps.exec.session_factory() as session:
            now = deps.exec.clock()
            inputs = deps.autonomy.inputs.load(session, now)
            _, outcome = autonomy_service.evaluate_and_apply(
                session, deps.autonomy.controller, inputs, now=now, audit=AuditLog(session, deps.exec.clock)
            )
            session.commit()
        report[f"autonomy_{outcome.label}"] += 1
    except Exception:
        log.exception("autonomy evaluation failed; the level is unchanged this cycle")
        report["autonomy_errors"] += 1


def _business_cycle(deps: LoopDeps, business_id: str, report: Counter[str]) -> None:
    now = deps.exec.clock()
    if deps.ingest is not None:
        with deps.exec.session_factory() as session:
            deps.ingest(session, business_id)
            session.commit()
    if deps.forecast is not None:
        _forecast(deps, business_id, now.date(), report)
    if deps.exec.world_provider is None:
        report["plan_skipped_no_world"] += 1
        return
    with deps.exec.session_factory() as session:
        world = deps.exec.world_provider.load(session, business_id)
        summary = run_plan(
            session, world=world, llm=deps.llm_factory(), ruleset=deps.exec.ruleset,
            audit=AuditLog(session, deps.exec.clock), now=now,
        )  # fmt: skip
        session.commit()
    report["proposals_created"] += len(summary.created)
    report["proposals_already_known"] += len(summary.already_known)


def _forecast(deps: LoopDeps, business_id: str, as_of: date, report: Counter[str]) -> None:
    assert deps.forecast is not None
    with deps.exec.session_factory() as session:
        results = list(deps.forecast(session, business_id, as_of))
        saved = repo.save_forecasts(session, as_of, results)
        fresh = {(r.currency, r.horizon_weeks, r.model_version) for r in saved}
        audit = AuditLog(session, deps.exec.clock)
        for result in results:
            if (result.currency, result.horizon_weeks, result.model_version) in fresh:
                audit.append(AuditEventType.FORECAST_MADE, audit_payloads.forecast_payload(result, as_of))
        session.commit()
    report["forecasts_saved"] += len(saved)


def _execute_approved(deps: LoopDeps, report: Counter[str]) -> None:
    with deps.exec.session_factory() as session:
        ids = [p.id for p in repo_exec.list_proposals_with_status(session, "APPROVED", limit=EXECUTE_BATCH)]
    for proposal_id in ids:
        try:
            report[f"exec_{process_proposal(deps.exec, proposal_id)}"] += 1
        except Exception:
            log.exception("execution step failed for %s", proposal_id)  # the key is stored: the next cycle replays it
            report["exec_errors"] += 1


def _reconcile(deps: LoopDeps, report: Counter[str]) -> None:
    """Apply webhook events that were stored before their execution could be matched."""
    now = deps.exec.clock()
    with deps.exec.session_factory() as session:
        audit = AuditLog(session, deps.exec.clock)
        for event in repo_exec.list_unmatched_events(session, since=now - deps.reconcile_window):
            outcome, execution_id = apply_event(
                session, audit, name=event.name, resource_id=event.resource_id, request_id=event.request_id, at=now
            )
            if outcome != "unmatched":
                event.outcome, event.execution_id = outcome, execution_id
                report[f"reconciled_{outcome}"] += 1
        session.commit()


def run_forever(deps: LoopDeps, interval_seconds: float, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            log.info("cycle: %s", dict(run_cycle(deps)))
        except Exception:
            log.exception("cycle crashed; trying again after the interval")
        stop.wait(interval_seconds)

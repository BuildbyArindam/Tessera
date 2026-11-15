"""Turn database rows into response bodies. The only place rows and schemas meet."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session
from tessera_core.domain import Currency, Money, SimulationSummary
from tessera_core.policy import PolicyContext
from tessera_store import models as m
from tessera_store import repo

from tessera_api.schemas import ApprovalOut, DecisionOut, ForecastOut, ProposalOut, SimulationOut


def forecast_out(row: m.Forecast) -> ForecastOut:
    currency = Currency(row.currency)
    return ForecastOut(
        id=row.id,
        business_id=row.business_id,
        currency=currency,
        horizon_weeks=row.horizon_weeks,
        as_of=row.as_of,
        point=Money(amount=row.point, currency=currency),
        lower=Money(amount=row.lower, currency=currency),
        upper=Money(amount=row.upper, currency=currency),
        target_coverage=row.target_coverage,
        model_version=row.model_version,
    )


def decision_out(row: m.PolicyDecision) -> DecisionOut:
    """The stored decision plus the two things on its context a person needs next to the verdict.

    The context is re-validated through the policy's own model, so what is shown is what the engine could have read.
    A context that no longer parses (or never existed) simply yields no evidence; the verdict and reasons still show.
    """
    base = DecisionOut.model_validate(row)
    if row.context is None:
        return base
    try:
        context = PolicyContext.model_validate(row.context)
    except ValueError:
        return base
    simulation = context.simulation
    widths = context.forecast_confidence
    return base.model_copy(
        update={
            "simulation": None if simulation is None else _simulation_out(simulation),
            "forecast_relative_width": None
            if widths is None
            else {currency: str(conf.relative_width) for currency, conf in widths.items()},
        }
    )


def _simulation_out(summary: SimulationSummary) -> SimulationOut:
    return SimulationOut(
        amount=summary.amount,
        expected_saving=summary.expected_saving,
        worst_case_loss=summary.worst_case_loss,
        n_paths=summary.n_paths,
    )


def proposal_outs(session: Session, rows: Sequence[m.Proposal]) -> list[ProposalOut]:
    ids = [row.id for row in rows]
    decisions = repo.latest_decisions(session, ids)
    approvals = repo.approvals_for(session, ids)
    out: list[ProposalOut] = []
    for row in rows:
        decision = decisions.get(row.id)
        approval = approvals.get(row.id)
        out.append(
            ProposalOut.model_validate(row).model_copy(
                update={
                    "decision": None if decision is None else decision_out(decision),
                    "approval": None if approval is None else ApprovalOut.model_validate(approval),
                }
            )
        )
    return out

"""Turn database rows into response bodies. The only place rows and schemas meet."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session
from tessera_core.domain import Currency, Money
from tessera_store import models as m
from tessera_store import repo

from tessera_api.schemas import ApprovalOut, DecisionOut, ForecastOut, ProposalOut


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
                    "decision": None if decision is None else DecisionOut.model_validate(decision),
                    "approval": None if approval is None else ApprovalOut.model_validate(approval),
                }
            )
        )
    return out

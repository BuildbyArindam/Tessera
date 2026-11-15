"""Read-only views of one business for the dashboard: who exists, what they hold, and what is coming due."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session
from tessera_store import repo, repo_dashboard

from tessera_api.deps import AppContext, get_ctx, get_session, require_api_key
from tessera_api.schemas import IDENT_PATTERN
from tessera_api.schemas_dashboard import BalancesOut, BusinessOut, ObligationsOut
from tessera_api.views_dashboard import balance_out, payable_out, receivable_out

router = APIRouter(prefix="/businesses", tags=["businesses"], dependencies=[Depends(require_api_key)])

BusinessId = Annotated[str, Path(min_length=1, max_length=64, pattern=IDENT_PATTERN)]


def _require_business(session: Session, business_id: str) -> None:
    if repo.get_business(session, business_id) is None:
        raise HTTPException(status_code=404, detail="unknown business_id")


@router.get("", response_model=list[BusinessOut])
def list_businesses(session: Session = Depends(get_session)) -> list[BusinessOut]:
    return [BusinessOut.model_validate(row) for row in repo_dashboard.list_businesses(session)]


@router.get("/{business_id}/balances", response_model=BalancesOut)
def get_balances(business_id: BusinessId, session: Session = Depends(get_session)) -> BalancesOut:
    """The latest balance per currency, as the worker last copied it from Airwallex. No rows: nothing ingested yet."""
    _require_business(session, business_id)
    rows = repo_dashboard.list_balances(session, business_id)
    return BalancesOut(business_id=business_id, balances=[balance_out(row) for row in rows])


@router.get("/{business_id}/obligations", response_model=ObligationsOut)
def get_obligations(
    business_id: BusinessId,
    days_ahead: int = Query(90, ge=1, le=180),
    as_of: date | None = None,
    limit: int = Query(100, ge=1, le=200),
    ctx: AppContext = Depends(get_ctx),
    session: Session = Depends(get_session),
) -> ObligationsOut:
    """Receivables (open invoices) and payables due within `days_ahead` of `as_of`, overdue ones included.

    `as_of` defaults to the date of the business's newest forecast, so "upcoming" is measured from the same day as the
    numbers beside it; with no forecast it is today.
    """
    _require_business(session, business_id)
    anchor = as_of or repo_dashboard.latest_as_of(session, business_id) or ctx.clock().date()
    horizon = anchor + timedelta(days=days_ahead)
    receivables = repo_dashboard.list_open_invoices_due(session, business_id, due_on_or_before=horizon, limit=limit)
    payables = repo_dashboard.list_open_payables(session, business_id, due_on_or_before=horizon, limit=limit)
    items = [receivable_out(row, anchor) for row in receivables] + [payable_out(row, anchor) for row in payables]
    items.sort(key=lambda o: (o.due_on, o.kind, o.id))
    return ObligationsOut(business_id=business_id, as_of=anchor, days_ahead=days_ahead, items=items[:limit])

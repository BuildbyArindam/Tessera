from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from tessera_core.domain import Currency
from tessera_store import repo, repo_dashboard

from tessera_api.deps import get_session, require_api_key
from tessera_api.schemas import ForecastOut, Ident
from tessera_api.views import forecast_out

router = APIRouter(prefix="/forecasts", tags=["forecasts"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=list[ForecastOut])
def list_forecasts(
    business_id: Ident | None = None,
    currency: Currency | None = None,
    latest: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> list[ForecastOut]:
    """Stored forecasts, newest `as_of` first. Each carries its interval and the target coverage it was built for.

    `latest=true` returns only the newest forecast per currency and horizon, which is what a dashboard draws. It needs
    `business_id`, because "newest" is per business. `limit` and `offset` do not apply to it: there is one row per
    currency and horizon, at most a few dozen.

    Each value is the NET cash flow expected in the single week `horizon_weeks` after `as_of` (inflows minus outflows
    in that currency), with an interval that should hold `target_coverage` of the time. It is not a running balance.
    """
    if latest:
        if business_id is None:
            raise HTTPException(status_code=422, detail="latest=true needs business_id")
        rows = repo_dashboard.latest_forecasts(session, business_id)
        return [forecast_out(row) for row in rows if currency is None or row.currency == currency.value]
    rows = repo.list_forecasts(
        session,
        business_id=business_id,
        currency=currency.value if currency else None,
        limit=limit,
        offset=offset,
    )
    return [forecast_out(row) for row in rows]

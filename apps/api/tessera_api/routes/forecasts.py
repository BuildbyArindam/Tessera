from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from tessera_core.domain import Currency
from tessera_store import repo

from tessera_api.deps import get_session, require_api_key
from tessera_api.schemas import ForecastOut, Ident
from tessera_api.views import forecast_out

router = APIRouter(prefix="/forecasts", tags=["forecasts"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=list[ForecastOut])
def list_forecasts(
    business_id: Ident | None = None,
    currency: Currency | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> list[ForecastOut]:
    """Stored forecasts, newest `as_of` first. Each carries its interval and the target coverage it was built for."""
    rows = repo.list_forecasts(
        session,
        business_id=business_id,
        currency=currency.value if currency else None,
        limit=limit,
        offset=offset,
    )
    return [forecast_out(row) for row in rows]

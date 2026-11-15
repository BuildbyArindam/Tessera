from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from tessera_core.domain import InvoiceStatus
from tessera_store import repo, repo_dashboard

from tessera_api.deps import get_session, require_api_key
from tessera_api.schemas import Ident, InvoiceIn, InvoiceOut
from tessera_api.schemas_dashboard import InvoiceRiskOut
from tessera_api.views_dashboard import invoice_risk_out

router = APIRouter(prefix="/invoices", tags=["invoices"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=list[InvoiceRiskOut])
def list_invoices(
    business_id: Ident,
    status: InvoiceStatus | None = None,
    order_by: repo_dashboard.InvoiceOrder = "due_on",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> list[InvoiceRiskOut]:
    """One business's invoices with their late-payment score. `order_by=risk` lists the riskiest first.

    `late_risk` is null for an invoice that has not been scored (paid invoices are not scored).
    """
    if repo.get_business(session, business_id) is None:
        raise HTTPException(status_code=404, detail="unknown business_id")
    rows = repo_dashboard.list_invoices_with_risk(
        session,
        business_id=business_id,
        status=status.value if status else None,
        order_by=order_by,
        limit=limit,
        offset=offset,
    )
    return [invoice_risk_out(invoice, risk) for invoice, risk in rows]


@router.post("", status_code=201, response_model=InvoiceOut)
def create_invoice(body: InvoiceIn, session: Session = Depends(get_session)) -> InvoiceOut:
    """Record a customer invoice. `notes` is untrusted free text: stored and returned as data, never interpreted."""
    invoice = body.to_domain()
    if repo.get_business(session, invoice.business_id) is None:
        raise HTTPException(status_code=404, detail="unknown business_id")
    try:
        row = repo.add_invoice(session, invoice)
    except repo.DuplicateError:
        raise HTTPException(status_code=409, detail="an invoice with this id already exists") from None
    session.commit()
    return InvoiceOut.model_validate(
        {
            "id": row.id,
            "business_id": row.business_id,
            "customer_id": row.customer_id,
            "amount": invoice.amount,
            "issued_on": row.issued_on,
            "due_on": row.due_on,
            "status": row.status,
            "notes": row.notes,
            "created_at": row.created_at,
        }
    )

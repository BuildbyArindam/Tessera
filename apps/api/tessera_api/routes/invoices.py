from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from tessera_store import repo

from tessera_api.deps import get_session, require_api_key
from tessera_api.schemas import InvoiceIn, InvoiceOut

router = APIRouter(prefix="/invoices", tags=["invoices"], dependencies=[Depends(require_api_key)])


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

"""Turn the dashboard's rows into response bodies. Kept apart from views.py so each file stays small."""

from __future__ import annotations

from datetime import date

from tessera_core.domain import Currency, Money
from tessera_store import models as m
from tessera_store import models_dashboard as d

from tessera_api.schemas_dashboard import BalanceOut, InvoiceRiskOut, LateRiskOut, ObligationOut


def balance_out(row: d.BalanceSnapshot) -> BalanceOut:
    return BalanceOut(available=Money(amount=row.available, currency=Currency(row.currency)), as_of=row.as_of)


def late_risk_out(row: d.LateRiskScore) -> LateRiskOut:
    return LateRiskOut(
        p_late=row.p_late,
        already_late=row.already_late,
        model=row.model,
        pay_date_low=row.pay_date_low,
        pay_date_median=row.pay_date_median,
        pay_date_high=row.pay_date_high,
        scored_as_of=row.scored_as_of,
    )


def invoice_risk_out(invoice: m.Invoice, risk: d.LateRiskScore | None) -> InvoiceRiskOut:
    return InvoiceRiskOut.model_validate(
        {
            "id": invoice.id,
            "business_id": invoice.business_id,
            "customer_id": invoice.customer_id,
            "amount": Money(amount=invoice.amount, currency=Currency(invoice.currency)),
            "issued_on": invoice.issued_on,
            "due_on": invoice.due_on,
            "status": invoice.status,
            "notes": invoice.notes,
            "created_at": invoice.created_at,
            "late_risk": None if risk is None else late_risk_out(risk),
        }
    )


def receivable_out(invoice: m.Invoice, as_of: date) -> ObligationOut:
    return ObligationOut(
        id=f"receivable:{invoice.id}",
        kind="RECEIVABLE",
        amount=Money(amount=invoice.amount, currency=Currency(invoice.currency)),
        due_on=invoice.due_on,
        days_until_due=(invoice.due_on - as_of).days,
        counterparty=invoice.customer_id,
        invoice_id=invoice.id,
    )


def payable_out(row: d.Payable, as_of: date) -> ObligationOut:
    return ObligationOut(
        id=f"payable:{row.id}",
        kind="PAYABLE",
        amount=Money(amount=row.amount, currency=Currency(row.currency)),
        due_on=row.due_on,
        days_until_due=(row.due_on - as_of).days,
        counterparty=row.counterparty_name or row.counterparty_id,
        payable_kind=row.kind,
    )

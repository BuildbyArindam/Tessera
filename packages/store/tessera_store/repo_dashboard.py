"""Queries behind the dashboard (Day 15). Same rules as repo.py: explicit, no business rules, never commit.

Reads serve the API. The `upsert_*` functions are the write side for whatever keeps the snapshots fresh (the worker's
ingest step and the seed script); each is idempotent, so running it twice leaves the same rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from tessera_store import models as m
from tessera_store import models_dashboard as d

InvoiceOrder = Literal["due_on", "risk"]


# ---- businesses -----------------------------------------------------------------------------------
def list_businesses(session: Session) -> Sequence[m.Business]:
    return session.scalars(select(m.Business).order_by(m.Business.id)).all()


# ---- forecasts: the newest set, one row per currency and horizon ------------------------------------
def latest_forecasts(session: Session, business_id: str) -> Sequence[m.Forecast]:
    """The newest forecast for each (currency, horizon). On the same day the later write wins."""
    stmt = (
        select(m.Forecast)
        .where(m.Forecast.business_id == business_id)
        .ext(distinct_on(m.Forecast.currency, m.Forecast.horizon_weeks))
        .order_by(m.Forecast.currency, m.Forecast.horizon_weeks, m.Forecast.as_of.desc(), m.Forecast.id.desc())
    )
    return session.scalars(stmt).all()


def latest_as_of(session: Session, business_id: str) -> date | None:
    return session.scalar(select(func.max(m.Forecast.as_of)).where(m.Forecast.business_id == business_id))


# ---- balances ---------------------------------------------------------------------------------------
def list_balances(session: Session, business_id: str) -> Sequence[d.BalanceSnapshot]:
    stmt = select(d.BalanceSnapshot).where(d.BalanceSnapshot.business_id == business_id)
    return session.scalars(stmt.order_by(d.BalanceSnapshot.currency)).all()


def upsert_balances(session: Session, business_id: str, balances: Mapping[str, Decimal], as_of: datetime) -> None:
    """Replace the snapshot for each currency given. Currencies not mentioned are left as they were."""
    rows = [
        {"business_id": business_id, "currency": currency, "available": amount, "as_of": as_of}
        for currency, amount in balances.items()
    ]
    if not rows:
        return
    stmt = pg_insert(d.BalanceSnapshot).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["business_id", "currency"],
        set_={"available": stmt.excluded.available, "as_of": stmt.excluded.as_of},
    )
    session.execute(stmt)


# ---- payables ---------------------------------------------------------------------------------------
def list_open_payables(
    session: Session, business_id: str, *, due_on_or_before: date, limit: int
) -> Sequence[d.Payable]:
    stmt = (
        select(d.Payable)
        .where(d.Payable.business_id == business_id, d.Payable.status == "OPEN", d.Payable.due_on <= due_on_or_before)
        .order_by(d.Payable.due_on, d.Payable.id)
        .limit(limit)
    )
    return session.scalars(stmt).all()


def upsert_payables(session: Session, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        return
    stmt = pg_insert(d.Payable).values(list(rows))
    keep = ("kind", "counterparty_id", "counterparty_name", "amount", "currency", "due_on", "status")
    session.execute(stmt.on_conflict_do_update(index_elements=["id"], set_={c: stmt.excluded[c] for c in keep}))


# ---- invoices with their late-payment score ---------------------------------------------------------
def list_invoices_with_risk(
    session: Session,
    *,
    business_id: str,
    status: str | None,
    order_by: InvoiceOrder,
    limit: int,
    offset: int,
) -> Sequence[tuple[m.Invoice, d.LateRiskScore | None]]:
    stmt = (
        select(m.Invoice, d.LateRiskScore)
        .outerjoin(d.LateRiskScore, d.LateRiskScore.invoice_id == m.Invoice.id)
        .where(m.Invoice.business_id == business_id)
    )
    if status is not None:
        stmt = stmt.where(m.Invoice.status == status)
    if order_by == "risk":  # riskiest first; invoices with no score last
        stmt = stmt.order_by(d.LateRiskScore.p_late.desc().nulls_last(), m.Invoice.due_on, m.Invoice.id)
    else:
        stmt = stmt.order_by(m.Invoice.due_on, m.Invoice.id)
    return [(inv, risk) for inv, risk in session.execute(stmt.limit(limit).offset(offset)).all()]


def list_open_invoices_due(
    session: Session, business_id: str, *, due_on_or_before: date, limit: int
) -> Sequence[m.Invoice]:
    """OPEN invoices due on or before a date, overdue ones included. These are the receivables."""
    stmt = (
        select(m.Invoice)
        .where(m.Invoice.business_id == business_id, m.Invoice.status == "OPEN", m.Invoice.due_on <= due_on_or_before)
        .order_by(m.Invoice.due_on, m.Invoice.id)
        .limit(limit)
    )
    return session.scalars(stmt).all()


def upsert_late_risk(session: Session, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        return
    stmt = pg_insert(d.LateRiskScore).values(list(rows))
    keep = ("p_late", "already_late", "model", "pay_date_low", "pay_date_median", "pay_date_high", "scored_as_of")
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["invoice_id"], set_={**{c: stmt.excluded[c] for c in keep}, "scored_at": func.now()}
        )
    )

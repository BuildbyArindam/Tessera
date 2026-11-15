"""Load the synthetic business into the database so the dashboard has something to show (Day 15).

    DATABASE_URL=postgresql+psycopg://... python scripts/seed_demo.py [--data-dir data/synthetic] [--seed 0]

It runs the real pipeline, not canned numbers: the Day 4 forecaster and the Day 5 late-payment scorer are fitted on the
synthetic history, and their output is stored. Safe to run twice: businesses, invoices, balances, payables and risk
scores are upserted, forecasts and their audit events are only added when new, so a second run changes nothing.

ASSUMPTION (docs/daily-log.md): the generator has no balance data, so each balance is a stand-in: four weeks of the
average gross weekly cash movement in that currency over the last 13 weeks. It exists so the demo has cards to draw.
The real source is the worker copying balances from Airwallex.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

sys.path[:0] = [f"packages/{p}" for p in ("core", "store", "audit", "evalkit")]

from tessera_audit import AuditEventType, AuditLog  # noqa: E402
from tessera_core.forecast.history import History  # noqa: E402
from tessera_evalkit import demo_models as dm  # noqa: E402
from tessera_evalkit.loading import load_open_payables  # noqa: E402
from tessera_store import models as m  # noqa: E402
from tessera_store import repo, repo_autonomy, repo_dashboard  # noqa: E402

INVOICE_STATUS = {  # Day 2 labels -> domain statuses. A part-paid invoice still has money owed, so it stays OPEN.
    "paid": "PAID", "open": "OPEN", "disputed": "DISPUTED", "written_off": "WRITTEN_OFF", "settled_partial": "OPEN",
}  # fmt: skip
BALANCE_WEEKS = 4
BALANCE_WINDOW_WEEKS = 13


def seed_businesses(session: Session, data_dir: Path) -> int:
    rows = pd.read_parquet(data_dir / "businesses.parquet")
    values = [{"id": r.business_id, "name": r.name, "base_currency": r.base_currency} for r in rows.itertuples()]
    session.execute(pg_insert(m.Business).values(values).on_conflict_do_nothing(index_elements=["id"]))
    return len(values)


def seed_invoices(session: Session, data_dir: Path) -> int:
    inv = pd.read_parquet(data_dir / "invoices.parquet")
    values = [
        {
            "id": r.invoice_id,
            "business_id": r.business_id,
            "customer_id": r.customer_id,
            "amount": Decimal(str(r.amount)),
            "currency": r.currency,
            "issued_on": r.invoice_date,
            "due_on": r.due_date,
            "status": INVOICE_STATUS[r.status],
            "notes": r.memo,
        }  # fmt: skip
        for r in inv.itertuples()
    ]
    for start in range(0, len(values), 1000):
        stmt = pg_insert(m.Invoice).values(values[start : start + 1000])
        session.execute(stmt.on_conflict_do_update(index_elements=["id"], set_={"status": stmt.excluded.status}))
    return len(values)


def seed_payables(session: Session, data_dir: Path) -> int:
    open_rows = load_open_payables(data_dir)
    values = [
        {
            "id": r.outflow_id,
            "business_id": r.business_id,
            "kind": r.kind,
            "counterparty_id": r.counterparty_id,
            "counterparty_name": None if pd.isna(r.counterparty_name) else str(r.counterparty_name),
            "amount": Decimal(str(r.amount)),
            "currency": r.currency,
            "due_on": r.due_date,
            "status": "OPEN",
        }  # fmt: skip
        for r in open_rows.itertuples()
    ]
    repo_dashboard.upsert_payables(session, values)
    return len(values)


def stand_in_balances(history: History, data_end: pd.Timestamp) -> dict[str, dict[str, Decimal]]:
    cut = data_end.normalize() - pd.Timedelta(weeks=BALANCE_WINDOW_WEEKS)
    recent = history.cashflows[history.cashflows["cash_date"] > cut]
    gross = recent.assign(a=recent["amount"].abs()).groupby(["business_id", "currency"])["a"].sum()
    out: dict[str, dict[str, Decimal]] = {}
    for (business, currency), total in gross.items():
        weekly = Decimal(str(round(float(total), 2))) / BALANCE_WINDOW_WEEKS
        out.setdefault(str(business), {})[str(currency)] = (weekly * BALANCE_WEEKS).quantize(Decimal("0.01"))
    return out


def seed_forecasts(session: Session, model: dm.DirectForecaster, history: History, end: pd.Timestamp) -> int:
    """Store the live forecasts and one hash-chained `forecast_made` audit event per NEW row."""
    as_of: date = end.date()
    saved = repo.save_forecasts(session, as_of, dm.live_forecasts(model, history, end))
    audit = AuditLog(session, clock=lambda: datetime.now(UTC))
    for row in saved:
        audit.append(
            AuditEventType.FORECAST_MADE,
            {
                "business_id": row.business_id, "currency": row.currency, "horizon_weeks": row.horizon_weeks,
                "as_of": as_of, "point": row.point, "lower": row.lower, "upper": row.upper,
                "target_coverage": row.target_coverage, "model_version": row.model_version,
            },
        )  # fmt: skip
    return len(saved)


def seed_late_risk(session: Session, data_dir: Path) -> int:
    scorer, invoices, end = dm.fit_late(data_dir)
    scored = dm.score_open_invoices(scorer, invoices, end)
    open_in_db = set(session.scalars(sa.select(m.Invoice.id).where(m.Invoice.status == "OPEN")))
    values = [
        {
            "invoice_id": a.invoice_id,
            "p_late": a.p_late,
            "already_late": a.already_late,
            "model": a.model,
            "pay_date_low": a.pay_date_low,
            "pay_date_median": a.pay_date_median,
            "pay_date_high": a.pay_date_high,
            "scored_as_of": a.as_of,
        }  # fmt: skip
        for a in scored
        if a.invoice_id in open_in_db
    ]
    repo_dashboard.upsert_late_risk(session, values)
    return len(values)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=Path("data/synthetic"))
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL must be set", file=sys.stderr)
        return 2

    history, end = dm.load_everything(args.data_dir)
    print(f"fitting on data up to {end.date()} ...", flush=True)
    model = dm.fit_forecaster(dm.build_training_frame(history, end), end, seed=args.seed)
    balances = stand_in_balances(history, end)

    engine = sa.create_engine(url)
    with Session(engine) as session, session.begin():
        counts = {
            "businesses": seed_businesses(session, args.data_dir),
            "invoices": seed_invoices(session, args.data_dir),
            "payables": seed_payables(session, args.data_dir),
            "late_risk_scores": seed_late_risk(session, args.data_dir),
            "new_forecasts": seed_forecasts(session, model, history, end),
        }
        at = datetime(end.year, end.month, end.day, tzinfo=UTC)
        for business, by_currency in balances.items():
            repo_dashboard.upsert_balances(session, business, by_currency, at)
        counts["balances"] = sum(len(v) for v in balances.values())
        repo_autonomy.get_state_row(session)  # make sure the autonomy dial exists (it starts at L0, advise-only)
        chain = AuditLog(session).verify_chain()
    engine.dispose()
    for name, n in counts.items():
        print(f"  {name:18s}{n:>7d}")
    print(f"audit chain intact: {chain.ok}")
    return 0 if chain.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

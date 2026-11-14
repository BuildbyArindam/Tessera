"""Day 11: the migrations build the schema the models describe, and the database enforces its own rules."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from it_helpers import ROOT
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tessera_store import models as m
from tessera_store.base import Base

EXPECTED_TABLES = {
    "businesses",
    "invoices",
    "payments",
    "forecasts",
    "proposals",
    "policy_decisions",
    "approvals",
    "executions",
    "audit_events",
    "webhook_events",
    "autonomy_state",
    "autonomy_promotions",
}  # fmt: skip  (nine from Day 11, webhook_events from Day 13, the two autonomy tables from Day 14)


def test_the_migration_creates_exactly_the_expected_tables(engine: sa.Engine) -> None:
    tables = set(sa.inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == EXPECTED_TABLES


def test_models_and_migration_agree(engine: sa.Engine) -> None:
    import tessera_audit.model  # noqa: F401  (registers audit_events)

    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []


def test_upgrade_downgrade_upgrade_round_trips(new_db: Callable[[], str]) -> None:
    url = new_db()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.downgrade(cfg, "base")
    eng = sa.create_engine(url)
    assert set(sa.inspect(eng).get_table_names()) == {"alembic_version"}
    with eng.connect() as conn:
        assert conn.scalar(sa.text("SELECT count(*) FROM pg_proc WHERE proname = 'audit_events_reject_change'")) == 0
    command.upgrade(cfg, "head")
    assert set(sa.inspect(eng).get_table_names()) - {"alembic_version"} == EXPECTED_TABLES
    eng.dispose()


def test_no_money_column_is_a_float(engine: sa.Engine) -> None:
    money = {("invoices", "amount"), ("payments", "amount"), ("forecasts", "point"), ("forecasts", "lower"),
             ("forecasts", "upper")}  # fmt: skip
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT table_name, column_name, data_type, numeric_precision, numeric_scale "
                "FROM information_schema.columns WHERE table_schema = 'public'"
            )
        ).all()
    found = {(r.table_name, r.column_name): r for r in rows}
    for key in money:
        col = found[key]
        assert (col.data_type, col.numeric_precision, col.numeric_scale) == ("numeric", 20, 2), key


def test_money_round_trips_exactly(session: Session) -> None:
    session.add(m.Business(id="B1", name="x", base_currency="INR"))
    session.add(
        m.Invoice(
            id="I1",
            business_id="B1",
            customer_id="C",
            amount=Decimal("0.10") + Decimal("0.20"),
            currency="USD",
            issued_on=date(2026, 1, 1),
            due_on=date(2026, 1, 2),
        )  # fmt: skip
    )
    session.commit()
    session.expire_all()
    assert session.get(m.Invoice, "I1").amount == Decimal("0.30")  # type: ignore[union-attr]


@pytest.fixture
def seeded(session: Session) -> Session:
    session.add(m.Business(id="B1", name="x", base_currency="INR"))
    session.add(m.Proposal(id="prp-1", business_id="B1", action_type="CONVERT", params={}, rationale="r",
                           confidence=0.5, status="PENDING_APPROVAL", plan_record=None,
                           proposed_at=sa.func.now()))  # fmt: skip
    session.commit()
    return session


def _invoice(**over: object) -> m.Invoice:
    base: dict[str, object] = dict(id="I1", business_id="B1", customer_id="C", amount=Decimal("10"), currency="USD",
                                   issued_on=date(2026, 1, 1), due_on=date(2026, 1, 31))  # fmt: skip
    return m.Invoice(**{**base, **over})


def _forecast(**over: object) -> m.Forecast:
    base: dict[str, object] = dict(business_id="B1", currency="USD", horizon_weeks=4, as_of=date(2026, 1, 1),
                                   point=Decimal("5"), lower=Decimal("1"), upper=Decimal("9"), target_coverage=0.8,
                                   model_version="v1")  # fmt: skip
    return m.Forecast(**{**base, **over})


def _proposal(**over: object) -> m.Proposal:
    base: dict[str, object] = dict(id="prp-x", business_id="B1", action_type="HOLD", params={}, rationale="r",
                                   confidence=0.5, status="BLOCKED", proposed_at=sa.func.now())  # fmt: skip
    return m.Proposal(**{**base, **over})


# (row that breaks exactly one rule, the constraint that must be the one that fires)
BAD_ROWS: dict[str, tuple[Callable[[], object], str]] = {
    "invoice amount zero": (lambda: _invoice(amount=Decimal("0")), "ck_invoices_amount_positive"),
    "invoice amount negative": (lambda: _invoice(amount=Decimal("-5")), "ck_invoices_amount_positive"),
    "invoice currency": (lambda: _invoice(currency="JPY"), "ck_invoices_currency"),
    "invoice due before issue": (lambda: _invoice(due_on=date(2025, 12, 31)), "ck_invoices_due_after_issue"),
    "invoice status": (lambda: _invoice(status="MAYBE"), "ck_invoices_status"),
    "invoice unknown business": (lambda: _invoice(business_id="NOPE"), "fk_invoices_business_id_businesses"),
    "forecast lower above point": (lambda: _forecast(lower=Decimal("6")), "ck_forecasts_interval_ordered"),
    "forecast upper below point": (lambda: _forecast(upper=Decimal("4")), "ck_forecasts_interval_ordered"),
    "forecast horizon zero": (lambda: _forecast(horizon_weeks=0), "ck_forecasts_horizon_positive"),
    "forecast coverage one": (lambda: _forecast(target_coverage=1.0), "ck_forecasts_coverage_range"),
    "forecast coverage zero": (lambda: _forecast(target_coverage=0.0), "ck_forecasts_coverage_range"),
    "forecast currency": (lambda: _forecast(currency="XXX"), "ck_forecasts_currency"),
    "payment direction": (
        lambda: m.Payment(id="P1", business_id="B1", direction="SIDEWAYS", amount=Decimal("1"), currency="USD",
                          counterparty_id="c", paid_on=date(2026, 1, 1)),
        "ck_payments_direction",
    ),
    "proposal action type": (lambda: _proposal(action_type="WIRE_ALL"), "ck_proposals_action_type"),
    "proposal confidence": (lambda: _proposal(confidence=1.5), "ck_proposals_confidence_range"),
    "proposal status": (lambda: _proposal(status="EXECUTED"), "ck_proposals_status"),
    "decision verdict": (
        lambda: m.PolicyDecision(proposal_id="prp-1", verdict="MAYBE", reasons=[], rule_ids=[]),
        "ck_policy_decisions_verdict",
    ),
    "decision phase": (
        lambda: m.PolicyDecision(proposal_id="prp-1", verdict="BLOCK", reasons=[], rule_ids=[], phase="later"),
        "ck_policy_decisions_phase",
    ),
    "approval decision": (
        lambda: m.Approval(proposal_id="prp-1", decision="SHRUG", actor="a"), "ck_approvals_decision"
    ),
    "execution status": (
        lambda: m.Execution(proposal_id="prp-1", idempotency_key="k", status="DONE"), "ck_executions_status"
    ),
    "execution unknown proposal": (
        lambda: m.Execution(proposal_id="NOPE", idempotency_key="k"), "fk_executions_proposal_id_proposals"
    ),
}  # fmt: skip


@pytest.mark.parametrize("case", BAD_ROWS)
def test_bad_rows_are_rejected_by_the_database_itself(seeded: Session, case: str) -> None:
    build, constraint = BAD_ROWS[case]
    seeded.add(build())
    with pytest.raises(IntegrityError, match=constraint):  # the intended rule fired, not some other column
        seeded.commit()
    seeded.rollback()


def test_a_proposal_can_be_approved_only_once_and_an_idempotency_key_used_only_once(seeded: Session) -> None:
    seeded.add(m.Approval(proposal_id="prp-1", decision="APPROVED", actor="operator"))
    seeded.commit()
    seeded.add(m.Approval(proposal_id="prp-1", decision="REJECTED", actor="operator"))
    with pytest.raises(IntegrityError):
        seeded.commit()
    seeded.rollback()
    seeded.add(m.Execution(proposal_id="prp-1", idempotency_key="k-1"))
    seeded.commit()
    seeded.add(m.Execution(proposal_id="prp-1", idempotency_key="k-1"))
    with pytest.raises(IntegrityError):
        seeded.commit()

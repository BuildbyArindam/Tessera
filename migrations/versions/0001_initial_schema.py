"""initial schema

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("seq", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('forecast_made', 'proposal_created', 'policy_decision', 'approval_requested', 'approved', 'rejected', 'execution_submitted', 'execution_result', 'webhook_received', 'autonomy_changed')",
            name=op.f("ck_audit_events_event_type"),
        ),
        sa.CheckConstraint("hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_audit_events_hash_format")),
        sa.CheckConstraint("prev_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_audit_events_prev_hash_format")),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_audit_events_seq_positive")),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_audit_events")),
        sa.UniqueConstraint("hash", name=op.f("uq_audit_events_hash")),
        sa.UniqueConstraint("prev_hash", name=op.f("uq_audit_events_prev_hash")),
    )
    op.create_table(
        "businesses",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "base_currency IN ('USD', 'EUR', 'GBP', 'INR', 'SGD', 'AUD')", name=op.f("ck_businesses_base_currency")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_businesses")),
    )
    op.create_table(
        "forecasts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("business_id", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("horizon_weeks", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("point", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("lower", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("upper", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("target_coverage", sa.Float(), nullable=False),
        sa.Column("model_version", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "currency IN ('USD', 'EUR', 'GBP', 'INR', 'SGD', 'AUD')", name=op.f("ck_forecasts_currency")
        ),
        sa.CheckConstraint("horizon_weeks > 0", name=op.f("ck_forecasts_horizon_positive")),
        sa.CheckConstraint("lower <= point AND point <= upper", name=op.f("ck_forecasts_interval_ordered")),
        sa.CheckConstraint("target_coverage > 0 AND target_coverage < 1", name=op.f("ck_forecasts_coverage_range")),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], name=op.f("fk_forecasts_business_id_businesses")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forecasts")),
        sa.UniqueConstraint(
            "business_id", "currency", "horizon_weeks", "as_of", "model_version", name="uq_forecasts_natural_key"
        ),
    )
    op.create_table(
        "invoices",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("business_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("issued_on", sa.Date(), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="OPEN", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("currency IN ('USD', 'EUR', 'GBP', 'INR', 'SGD', 'AUD')", name=op.f("ck_invoices_currency")),
        sa.CheckConstraint("status IN ('OPEN', 'PAID', 'DISPUTED', 'WRITTEN_OFF')", name=op.f("ck_invoices_status")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_invoices_amount_positive")),
        sa.CheckConstraint("due_on >= issued_on", name=op.f("ck_invoices_due_after_issue")),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], name=op.f("fk_invoices_business_id_businesses")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoices")),
    )
    op.create_index(
        "ix_invoices_business_id_status_due_on", "invoices", ["business_id", "status", "due_on"], unique=False
    )
    op.create_table(
        "proposals",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("business_id", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.String(length=16), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("plan_record", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "action_type IN ('CONVERT', 'HOLD', 'PAY_SUPPLIER', 'TOPUP_CARD', 'FLAG_INVOICE')",
            name=op.f("ck_proposals_action_type"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING_APPROVAL', 'APPROVED', 'REJECTED', 'BLOCKED')", name=op.f("ck_proposals_status")
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name=op.f("ck_proposals_confidence_range")),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], name=op.f("fk_proposals_business_id_businesses")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proposals")),
    )
    op.create_index("ix_proposals_business_id_status", "proposals", ["business_id", "status"], unique=False)
    op.create_table(
        "approvals",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("proposal_id", sa.String(length=128), nullable=False),
        sa.Column("decision", sa.String(length=10), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("decision IN ('APPROVED', 'REJECTED')", name=op.f("ck_approvals_decision")),
        sa.ForeignKeyConstraint(["proposal_id"], ["proposals.id"], name=op.f("fk_approvals_proposal_id_proposals")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
        sa.UniqueConstraint("proposal_id", name=op.f("uq_approvals_proposal_id")),
    )
    op.create_table(
        "executions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("proposal_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="PENDING", nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SUBMITTED', 'SETTLED', 'FAILED')", name=op.f("ck_executions_status")
        ),
        sa.ForeignKeyConstraint(["proposal_id"], ["proposals.id"], name=op.f("fk_executions_proposal_id_proposals")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_executions")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_executions_idempotency_key")),
    )
    op.create_index("ix_executions_proposal_id", "executions", ["proposal_id"], unique=False)
    op.create_table(
        "payments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("business_id", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("counterparty_id", sa.String(length=64), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("invoice_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("currency IN ('USD', 'EUR', 'GBP', 'INR', 'SGD', 'AUD')", name=op.f("ck_payments_currency")),
        sa.CheckConstraint("direction IN ('INBOUND', 'OUTBOUND')", name=op.f("ck_payments_direction")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payments_amount_positive")),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], name=op.f("fk_payments_business_id_businesses")),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], name=op.f("fk_payments_invoice_id_invoices")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
    )
    op.create_index("ix_payments_business_id_paid_on", "payments", ["business_id", "paid_on"], unique=False)
    op.create_table(
        "policy_decisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("proposal_id", sa.String(length=128), nullable=False),
        sa.Column("phase", sa.String(length=10), server_default="plan", nullable=False),
        sa.Column("verdict", sa.String(length=10), nullable=False),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rule_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ruleset_hash", sa.String(length=64), nullable=True),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("phase IN ('plan', 'execution')", name=op.f("ck_policy_decisions_phase")),
        sa.CheckConstraint("verdict IN ('APPROVE', 'ESCALATE', 'BLOCK')", name=op.f("ck_policy_decisions_verdict")),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["proposals.id"], name=op.f("fk_policy_decisions_proposal_id_proposals")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_decisions")),
    )
    op.create_index("ix_policy_decisions_proposal_id", "policy_decisions", ["proposal_id"], unique=False)

    # audit_events is append-only. A row trigger rejects UPDATE and DELETE; a statement trigger rejects TRUNCATE.
    # The table's owner or a superuser can still drop or disable these triggers, so the application should connect
    # as a role that owns nothing here (see docs/audit-log.md).
    op.execute(
        """
        CREATE FUNCTION audit_events_reject_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_events_no_update_delete BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_reject_change()"
    )
    op.execute(
        "CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_events_reject_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_events_no_truncate ON audit_events")
    op.execute("DROP TRIGGER audit_events_no_update_delete ON audit_events")
    op.execute("DROP FUNCTION audit_events_reject_change()")
    op.drop_index("ix_policy_decisions_proposal_id", table_name="policy_decisions")
    op.drop_table("policy_decisions")
    op.drop_index("ix_payments_business_id_paid_on", table_name="payments")
    op.drop_table("payments")
    op.drop_index("ix_executions_proposal_id", table_name="executions")
    op.drop_table("executions")
    op.drop_table("approvals")
    op.drop_index("ix_proposals_business_id_status", table_name="proposals")
    op.drop_table("proposals")
    op.drop_index("ix_invoices_business_id_status_due_on", table_name="invoices")
    op.drop_table("invoices")
    op.drop_table("forecasts")
    op.drop_table("businesses")
    op.drop_table("audit_events")

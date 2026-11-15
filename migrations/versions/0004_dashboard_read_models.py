"""dashboard read models: balance snapshots, payables and late-payment risk scores

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENCIES = "currency IN ('USD', 'EUR', 'GBP', 'INR', 'SGD', 'AUD')"


def upgrade() -> None:
    op.create_table(
        "balance_snapshots",
        sa.Column("business_id", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("available", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(CURRENCIES, name=op.f("ck_balance_snapshots_currency")),
        sa.CheckConstraint("available >= 0", name=op.f("ck_balance_snapshots_available_nonnegative")),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], name=op.f("fk_balance_snapshots_business_id_businesses")
        ),
        sa.PrimaryKeyConstraint("business_id", "currency", name=op.f("pk_balance_snapshots")),
    )
    op.create_table(
        "payables",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("business_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("counterparty_id", sa.String(length=64), nullable=False),
        sa.Column("counterparty_name", sa.String(length=200), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=8), server_default="OPEN", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('payroll', 'supplier_invoice', 'saas_card')", name=op.f("ck_payables_kind")),
        sa.CheckConstraint(CURRENCIES, name=op.f("ck_payables_currency")),
        sa.CheckConstraint("status IN ('OPEN', 'PAID')", name=op.f("ck_payables_status")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payables_amount_positive")),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], name=op.f("fk_payables_business_id_businesses")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payables")),
    )
    op.create_index("ix_payables_business_id_status_due_on", "payables", ["business_id", "status", "due_on"])
    op.create_table(
        "late_risk_scores",
        sa.Column("invoice_id", sa.String(length=64), nullable=False),
        sa.Column("p_late", sa.Float(), nullable=False),
        sa.Column("already_late", sa.Boolean(), nullable=False),
        sa.Column("model", sa.String(length=16), nullable=False),
        sa.Column("pay_date_low", sa.Date(), nullable=True),
        sa.Column("pay_date_median", sa.Date(), nullable=True),
        sa.Column("pay_date_high", sa.Date(), nullable=True),
        sa.Column("scored_as_of", sa.Date(), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("p_late >= 0 AND p_late <= 1", name=op.f("ck_late_risk_scores_p_late_range")),
        sa.CheckConstraint("model IN ('logreg', 'lgbm', 'deterministic')", name=op.f("ck_late_risk_scores_model")),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], name=op.f("fk_late_risk_scores_invoice_id_invoices")),
        sa.PrimaryKeyConstraint("invoice_id", name=op.f("pk_late_risk_scores")),
    )


def downgrade() -> None:
    op.drop_table("late_risk_scores")
    op.drop_index("ix_payables_business_id_status_due_on", table_name="payables")
    op.drop_table("payables")
    op.drop_table("balance_snapshots")

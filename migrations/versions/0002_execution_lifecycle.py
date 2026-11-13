"""execution lifecycle: SUBMITTED/SETTLED/FAILED proposals, one execution per proposal, webhook_events

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "status IN ('PENDING_APPROVAL', 'APPROVED', 'REJECTED', 'BLOCKED')"
NEW = "status IN ('PENDING_APPROVAL', 'APPROVED', 'REJECTED', 'BLOCKED', 'SUBMITTED', 'SETTLED', 'FAILED')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_proposals_status"), "proposals", type_="check")
    op.create_check_constraint(op.f("ck_proposals_status"), "proposals", NEW)

    op.add_column("executions", sa.Column("vendor_request_id", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_executions_proposal_id", "executions", ["proposal_id"])
    op.drop_index("ix_executions_proposal_id", table_name="executions")  # the unique constraint's index covers it
    op.create_index("ix_executions_external_id", "executions", ["external_id"], unique=False)
    op.create_index("ix_executions_vendor_request_id", "executions", ["vendor_request_id"], unique=False)

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("event_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("outcome", sa.String(length=10), nullable=False),
        sa.Column("execution_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("outcome IN ('applied', 'ignored', 'unmatched')", name=op.f("ck_webhook_events_outcome")),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["executions.id"], name=op.f("fk_webhook_events_execution_id_executions")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_events")),
    )
    op.create_index("ix_webhook_events_outcome", "webhook_events", ["outcome"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    stuck = bind.scalar(sa.text("SELECT count(*) FROM proposals WHERE status IN ('SUBMITTED', 'SETTLED', 'FAILED')"))
    if stuck:
        raise RuntimeError(f"cannot downgrade: {stuck} proposals are SUBMITTED, SETTLED or FAILED and 0001 has no such status")
    op.drop_table("webhook_events")
    op.drop_index("ix_executions_vendor_request_id", table_name="executions")
    op.drop_index("ix_executions_external_id", table_name="executions")
    op.drop_constraint("uq_executions_proposal_id", "executions", type_="unique")
    op.create_index("ix_executions_proposal_id", "executions", ["proposal_id"], unique=False)
    op.drop_column("executions", "vendor_request_id")
    op.drop_constraint(op.f("ck_proposals_status"), "proposals", type_="check")
    op.create_check_constraint(op.f("ck_proposals_status"), "proposals", OLD)

"""autonomy dial: the one-row level state and the promotions the controller proposes

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "autonomy_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_autonomy_state_single_row")),
        sa.CheckConstraint("level >= 0 AND level <= 3", name=op.f("ck_autonomy_state_level")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_autonomy_state")),
    )
    # Tessera starts advise-only: nothing executes until the dial has been earned and a person has confirmed it.
    op.execute("INSERT INTO autonomy_state (id, level, since) VALUES (1, 0, now())")
    op.create_table(
        "autonomy_promotions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("from_level", sa.Integer(), nullable=False),
        sa.Column("to_level", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="PENDING", nullable=False),
        sa.Column("assessment", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'CONFIRMED', 'REJECTED', 'CANCELLED')", name=op.f("ck_autonomy_promotions_status")
        ),
        sa.CheckConstraint("from_level >= 0 AND to_level <= 3", name=op.f("ck_autonomy_promotions_level_range")),
        sa.CheckConstraint("to_level = from_level + 1", name=op.f("ck_autonomy_promotions_one_step")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_autonomy_promotions")),
    )
    op.create_index(
        "uq_autonomy_promotions_one_pending",
        "autonomy_promotions",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_index("uq_autonomy_promotions_one_pending", table_name="autonomy_promotions")
    op.drop_table("autonomy_promotions")
    op.drop_table("autonomy_state")

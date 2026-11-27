"""events table (device/ops event log) and telemetry.segment_id

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-26 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("aircraft_id", sa.String(length=32), nullable=True),
        sa.Column("message", sa.String(length=256), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_by", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_events_at"), ["at"], unique=False)
        batch_op.create_index(batch_op.f("ix_events_kind"), ["kind"], unique=False)
    with op.batch_alter_table("telemetry", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("segment_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("telemetry", schema=None) as batch_op:
        batch_op.drop_column("segment_id")
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_events_kind"))
        batch_op.drop_index(batch_op.f("ix_events_at"))
    op.drop_table("events")

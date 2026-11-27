"""AI reroute proposals (U1): the reroutes table

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-26 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reroutes",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("flight_id", sa.String(length=36), nullable=False),
        sa.Column("aircraft_id", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("callsign", sa.String(length=16), nullable=True),
        sa.Column("destination", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("trigger", sa.String(length=12), nullable=False),
        sa.Column("reason", sa.String(length=400), nullable=False),
        sa.Column("gain_s", sa.Float(), nullable=False),
        sa.Column("current", sa.JSON(), nullable=False),
        sa.Column("proposed", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.String(length=64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("reroutes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_reroutes_flight_id"), ["flight_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_reroutes_aircraft_id"), ["aircraft_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_reroutes_status"), ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("reroutes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_reroutes_status"))
        batch_op.drop_index(batch_op.f("ix_reroutes_aircraft_id"))
        batch_op.drop_index(batch_op.f("ix_reroutes_flight_id"))
    op.drop_table("reroutes")

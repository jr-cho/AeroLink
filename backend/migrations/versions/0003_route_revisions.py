"""route_revisions: persisted route-response revisions

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-26 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route_revisions",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("route_revisions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_route_revisions_request_id"), ["request_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("route_revisions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_route_revisions_request_id"))
    op.drop_table("route_revisions")

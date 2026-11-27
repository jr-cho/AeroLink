"""pairing-code hash per device, screen sessions

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-25 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pairing_hash", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("pairing_salt", sa.String(length=64), nullable=True))
    op.create_table(
        "screen_sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("device_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    with op.batch_alter_table("screen_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_screen_sessions_device_id"), ["device_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("screen_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_screen_sessions_device_id"))
    op.drop_table("screen_sessions")
    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.drop_column("pairing_salt")
        batch_op.drop_column("pairing_hash")

"""ATC operator accounts and sessions; unit callsign

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-26 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operators",
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=128), nullable=False),
        sa.Column("password_salt", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("username"),
    )
    op.create_table(
        "operator_sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    with op.batch_alter_table("operator_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_operator_sessions_username"), ["username"], unique=False)
    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("callsign", sa.String(length=10), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.drop_column("callsign")
    with op.batch_alter_table("operator_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_operator_sessions_username"))
    op.drop_table("operator_sessions")
    op.drop_table("operators")

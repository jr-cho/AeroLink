"""ATC clearance marks (E6.14)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-26 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clearances",
        sa.Column("flight_id", sa.String(length=36), nullable=False),
        sa.Column("requirement", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("operator", sa.String(length=64), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("flight_id"),
    )


def downgrade() -> None:
    op.drop_table("clearances")

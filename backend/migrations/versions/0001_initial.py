"""initial schema: devices, route requests, planned flights, telemetry

Revision ID: 0001
Revises:
Create Date: 2026-11-25 13:25:39.264677
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("vehicle_type", sa.String(length=16), nullable=False),
        sa.Column("demo", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("online", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "planned_flights",
        sa.Column("flight_id", sa.String(length=36), nullable=False),
        sa.Column("aircraft_id", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("route_id", sa.String(length=36), nullable=False),
        sa.Column("vehicle_type", sa.String(length=16), nullable=False),
        sa.Column("path", sa.JSON(), nullable=False),
        sa.Column("destination", sa.String(length=64), nullable=False),
        sa.Column("departure_s", sa.Float(), nullable=False),
        sa.Column("eta_by_node", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("flight_id"),
    )
    with op.batch_alter_table("planned_flights", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_planned_flights_aircraft_id"), ["aircraft_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_planned_flights_status"), ["status"], unique=False)

    op.create_table(
        "route_requests",
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("aircraft_id", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("vehicle_type", sa.String(length=16), nullable=False),
        sa.Column("destination", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=64), nullable=True),
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("request_id"),
    )
    with op.batch_alter_table("route_requests", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_route_requests_aircraft_id"), ["aircraft_id"], unique=False)

    op.create_table(
        "telemetry",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("aircraft_id", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("alt_msl_m", sa.Float(), nullable=False),
        sa.Column("ground_speed_mps", sa.Float(), nullable=False),
        sa.Column("course_deg", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("telemetry", schema=None) as batch_op:
        batch_op.create_index("ix_telemetry_aircraft_time", ["aircraft_id", "timestamp"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("telemetry", schema=None) as batch_op:
        batch_op.drop_index("ix_telemetry_aircraft_time")

    op.drop_table("telemetry")
    with op.batch_alter_table("route_requests", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_route_requests_aircraft_id"))

    op.drop_table("route_requests")
    with op.batch_alter_table("planned_flights", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_planned_flights_status"))
        batch_op.drop_index(batch_op.f("ix_planned_flights_aircraft_id"))

    op.drop_table("planned_flights")
    op.drop_table("devices")

"""Database schema (SQLAlchemy Core). Alembic migrations in backend/migrations/ are generated from this.

Runs on PostgreSQL in production and SQLite in tests: only portable types are used.
"""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()
TS = sa.DateTime(timezone=True)

devices = sa.Table(
    "devices",
    metadata,
    sa.Column("id", sa.String(32), primary_key=True),  # = MQTT username = aircraft_id
    sa.Column("vehicle_type", sa.String(16), nullable=False),
    sa.Column("demo", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("registered_at", TS, nullable=False),
    sa.Column("last_seen", TS),
    sa.Column("online", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("pairing_hash", sa.String(128)),  # scrypt of the pairing code (hex); the code is never stored
    sa.Column("pairing_salt", sa.String(64)),  # per-device random salt (hex)
    sa.Column("callsign", sa.String(10)),  # the unit's radio callsign (G2); null for rows registered before it existed
)

# A paired screen's session. Only the SHA-256 of the cookie token is stored, so a database leak
# can't be replayed as a login. Sessions live in the database so they survive a backend restart.
screen_sessions = sa.Table(
    "screen_sessions",
    metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True),
    sa.Column("device_id", sa.String(32), nullable=False, index=True),
    sa.Column("created_at", TS, nullable=False),
    sa.Column("expires_at", TS, nullable=False),
)

# ATC operator accounts (G2). The password is stored as a scrypt hash with a per-user salt, never in clear.
operators = sa.Table(
    "operators",
    metadata,
    sa.Column("username", sa.String(64), primary_key=True),  # lower case; the primary key makes it unique
    sa.Column("display_name", sa.String(64), nullable=False),
    sa.Column("password_hash", sa.String(128), nullable=False),
    sa.Column("password_salt", sa.String(64), nullable=False),
    sa.Column("created_at", TS, nullable=False),
    sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("last_login", TS),
)

# An operator's login session (12 h). Only the SHA-256 of the cookie token is stored; sessions survive a restart.
operator_sessions = sa.Table(
    "operator_sessions",
    metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True),
    sa.Column("username", sa.String(64), nullable=False, index=True),
    sa.Column("created_at", TS, nullable=False),
    sa.Column("expires_at", TS, nullable=False),
)

route_requests = sa.Table(
    "route_requests",
    metadata,
    sa.Column("request_id", sa.String(36), primary_key=True),
    sa.Column("aircraft_id", sa.String(32), nullable=False, index=True),
    sa.Column("source", sa.String(8), nullable=False),
    sa.Column("vehicle_type", sa.String(16), nullable=False),
    sa.Column("destination", sa.String(64), nullable=False),
    sa.Column("origin", sa.String(64)),  # snapped origin node; null when refused
    sa.Column("revision", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("created_at", TS, nullable=False),
    sa.Column("response", sa.JSON, nullable=False),  # the RouteResponse as sent
)

# Every later response to a request (en-route suggestions, off-route recalculation, LLM summary) is a
# revision. route_requests keeps only the first response, so revisions live here and a pilot can still
# select from any of them after a backend restart.
route_revisions = sa.Table(
    "route_revisions",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
    sa.Column("request_id", sa.String(36), nullable=False, index=True),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("origin", sa.String(64)),  # first node of the revised routes
    sa.Column("created_at", TS, nullable=False),
    sa.Column("response", sa.JSON, nullable=False),
)

planned_flights = sa.Table(
    "planned_flights",
    metadata,
    sa.Column("flight_id", sa.String(36), primary_key=True),
    sa.Column("aircraft_id", sa.String(32), nullable=False, index=True),
    sa.Column("source", sa.String(8), nullable=False),
    sa.Column("request_id", sa.String(36), nullable=False),
    sa.Column("route_id", sa.String(36), nullable=False),
    sa.Column("vehicle_type", sa.String(16), nullable=False),
    sa.Column("path", sa.JSON, nullable=False),
    sa.Column("destination", sa.String(64), nullable=False),
    sa.Column("departure_s", sa.Float, nullable=False),  # unix seconds
    sa.Column("eta_by_node", sa.JSON, nullable=False),  # node -> unix seconds (p50)
    sa.Column("status", sa.String(16), nullable=False, index=True),
    sa.Column("updated_at", TS, nullable=False),
)

telemetry = sa.Table(
    "telemetry",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
    sa.Column("aircraft_id", sa.String(32), nullable=False),
    sa.Column("source", sa.String(8), nullable=False),
    sa.Column("timestamp", TS, nullable=False),
    sa.Column("lat", sa.Float, nullable=False),
    sa.Column("lon", sa.Float, nullable=False),
    sa.Column("alt_msl_m", sa.Float, nullable=False),
    sa.Column("ground_speed_mps", sa.Float, nullable=False),
    sa.Column("course_deg", sa.Float, nullable=False),
    # Trails and off-route detection never join across a segment (reconnect, gap > 30 s, GNSS fix recovery).
    # Null for rows written before segments existed. Value: unix ms of the segment's first sample.
    sa.Column("segment_id", sa.BigInteger().with_variant(sa.Integer, "sqlite")),
    sa.Index("ix_telemetry_aircraft_time", "aircraft_id", "timestamp"),
)

# Device and operations event log (E6.9): unit online/offline, degraded, GNSS fix, off-route, watchdog changes,
# operator acknowledgements. An alert is an event that has not been acknowledged yet.
events = sa.Table(
    "events",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
    sa.Column("at", TS, nullable=False, index=True),
    sa.Column("kind", sa.String(32), nullable=False, index=True),
    sa.Column("severity", sa.String(8), nullable=False),  # info | warn | critical
    sa.Column("aircraft_id", sa.String(32)),
    sa.Column("message", sa.String(256), nullable=False),
    sa.Column("detail", sa.JSON, nullable=False),
    sa.Column("acked_at", TS),
    sa.Column("acked_by", sa.String(64)),
)

# ATC's record that a flight's airspace requirement was handled by radio (E6.14): "cleared" (Class B clearance) or
# "contacted" (Class C/D contact). Informational: nothing in routing or navigation reads it. A row only counts while
# its ``requirement`` still equals the flight's current one, so a reroute that changes the airspace starts again.
clearances = sa.Table(
    "clearances",
    metadata,
    sa.Column("flight_id", sa.String(36), primary_key=True),
    sa.Column("requirement", sa.String(24), nullable=False),
    sa.Column("status", sa.String(12), nullable=False),  # cleared | contacted
    sa.Column("operator", sa.String(64), nullable=False),
    sa.Column("at", TS, nullable=False),
)

# AI reroute proposals (U1, contract §2): the AI proposes, ATC approves, the pilot acknowledges or refuses. Each row is
# one proposal; ``current``/``proposed`` are JSON (paths, waypoints, times) and ``events`` is its ordered history.
reroutes = sa.Table(
    "reroutes",
    metadata,
    sa.Column("id", sa.String(16), primary_key=True),
    sa.Column("flight_id", sa.String(36), nullable=False, index=True),
    sa.Column("aircraft_id", sa.String(32), nullable=False, index=True),
    sa.Column("source", sa.String(8), nullable=False),  # aircraft (a unit with a pilot screen) | sim
    sa.Column("callsign", sa.String(16)),
    sa.Column("destination", sa.String(64), nullable=False),
    sa.Column(
        "status", sa.String(12), nullable=False, index=True
    ),  # proposed|approved|acknowledged|unable|dismissed|expired
    sa.Column("trigger", sa.String(12), nullable=False),  # faster | conflict | medical
    sa.Column("reason", sa.String(400), nullable=False),
    sa.Column("gain_s", sa.Float, nullable=False),
    sa.Column("current", sa.JSON, nullable=False),
    sa.Column("proposed", sa.JSON, nullable=False),
    sa.Column("created_at", TS, nullable=False),
    sa.Column("expires_at", TS, nullable=False),
    sa.Column("approved_by", sa.String(64)),
    sa.Column("approved_at", TS),
    sa.Column("responded_at", TS),
    sa.Column("events", sa.JSON, nullable=False),
)

ACTIVE_STATUSES = ("planned", "active", "off_route")

"""Async persistence: devices, route requests, planned flights, telemetry."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aerolink.service.db import (
    ACTIVE_STATUSES,
    clearances,
    devices,
    events,
    metadata,
    operator_sessions,
    operators,
    planned_flights,
    reroutes,
    route_requests,
    route_revisions,
    screen_sessions,
    telemetry,
)

# Never returned by the general device queries (they feed public endpoints such as /api/devices).
SECRET_DEVICE_COLUMNS = ("pairing_hash", "pairing_salt")
PUBLIC_DEVICE_COLUMNS = [c for c in devices.c if c.name not in SECRET_DEVICE_COLUMNS]


def now() -> datetime:
    return datetime.now(UTC)


class Store:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    @classmethod
    def connect(cls, url: str) -> Store:
        return cls(create_async_engine(url, pool_pre_ping=True))

    async def create_all(self) -> None:
        """Tests and throwaway dev databases only; real databases use Alembic."""
        async with self.engine.begin() as c:
            await c.run_sync(metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    # --- devices -----------------------------------------------------------------
    async def register_device(
        self, device_id: str, vehicle_type: str, demo: bool = False, callsign: str | None = None
    ) -> None:
        async with self.engine.begin() as c:
            await c.execute(sa.delete(devices).where(devices.c.id == device_id))
            await c.execute(sa.delete(screen_sessions).where(screen_sessions.c.device_id == device_id))
            await c.execute(
                sa.insert(devices).values(
                    id=device_id, vehicle_type=vehicle_type, demo=demo, registered_at=now(), callsign=callsign
                )
            )

    async def set_callsign(self, device_id: str, callsign: str) -> bool:
        """False if the unit is not registered."""
        async with self.engine.begin() as c:
            res = await c.execute(sa.update(devices).where(devices.c.id == device_id).values(callsign=callsign))
            return res.rowcount == 1

    async def get_device(self, device_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as c:
            row = (
                (await c.execute(sa.select(*PUBLIC_DEVICE_COLUMNS).where(devices.c.id == device_id))).mappings().first()
            )
        return dict(row) if row else None

    async def list_devices(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as c:
            rows = (await c.execute(sa.select(*PUBLIC_DEVICE_COLUMNS).order_by(devices.c.id))).mappings().all()
        return [dict(r) for r in rows]

    async def update_device(self, device_id: str, **values: Any) -> None:
        async with self.engine.begin() as c:
            await c.execute(sa.update(devices).where(devices.c.id == device_id).values(**values))

    # --- pairing and screen sessions (ARCHITECTURE §14.5) ----------------------------------------
    async def set_pairing(self, device_id: str, code_hash: str, salt: str) -> bool:
        """Store a new pairing-code hash and end every session of the device. False if unknown."""
        async with self.engine.begin() as c:
            q = sa.update(devices).where(devices.c.id == device_id)
            if (await c.execute(q.values(pairing_hash=code_hash, pairing_salt=salt))).rowcount == 0:
                return False
            await c.execute(sa.delete(screen_sessions).where(screen_sessions.c.device_id == device_id))
        return True

    async def get_pairing(self, device_id: str) -> tuple[str, str] | None:
        """(hash, salt) of the device's pairing code, or None if unknown or never provisioned."""
        async with self.engine.connect() as c:
            q = sa.select(devices.c.pairing_hash, devices.c.pairing_salt).where(devices.c.id == device_id)
            row = (await c.execute(q)).first()
        return (row[0], row[1]) if row and row[0] and row[1] else None

    async def add_session(self, token_hash: str, device_id: str, expires_at: datetime) -> None:
        async with self.engine.begin() as c:
            await c.execute(sa.delete(screen_sessions).where(screen_sessions.c.expires_at < now()))
            await c.execute(
                sa.insert(screen_sessions).values(
                    token_hash=token_hash, device_id=device_id, created_at=now(), expires_at=expires_at
                )
            )

    async def session_valid(self, token_hash: str, device_id: str) -> bool:
        q = sa.select(screen_sessions.c.expires_at).where(
            screen_sessions.c.token_hash == token_hash, screen_sessions.c.device_id == device_id
        )
        async with self.engine.connect() as c:
            row = (await c.execute(q)).first()
        if row is None:
            return False
        expires = row[0] if row[0].tzinfo else row[0].replace(tzinfo=UTC)  # SQLite drops the zone
        return expires > now()

    async def end_sessions(self, device_id: str) -> None:
        async with self.engine.begin() as c:
            await c.execute(sa.delete(screen_sessions).where(screen_sessions.c.device_id == device_id))

    # --- ATC operators and their sessions (G2) ---------------------------------------------------
    async def add_operator(self, username: str, display_name: str, password_hash: str, salt: str) -> bool:
        """False if the username is taken."""
        try:
            async with self.engine.begin() as c:
                await c.execute(
                    sa.insert(operators).values(
                        username=username,
                        display_name=display_name,
                        password_hash=password_hash,
                        password_salt=salt,
                        created_at=now(),
                    )
                )
            return True
        except IntegrityError:
            return False

    async def get_operator(self, username: str) -> dict[str, Any] | None:
        """The full row, hash included: only the login check and the CLI may call this."""
        async with self.engine.connect() as c:
            row = (await c.execute(sa.select(operators).where(operators.c.username == username))).mappings().first()
        return dict(row) if row else None

    async def list_operators(self) -> list[dict[str, Any]]:
        cols = [c for c in operators.c if c.name not in ("password_hash", "password_salt")]
        async with self.engine.connect() as c:
            rows = (await c.execute(sa.select(*cols).order_by(operators.c.username))).mappings().all()
        return [dict(r) for r in rows]

    async def set_operator_password(self, username: str, password_hash: str, salt: str) -> bool:
        """New password; ends the operator's sessions. False if unknown."""
        async with self.engine.begin() as c:
            q = sa.update(operators).where(operators.c.username == username)
            if (await c.execute(q.values(password_hash=password_hash, password_salt=salt))).rowcount == 0:
                return False
            await c.execute(sa.delete(operator_sessions).where(operator_sessions.c.username == username))
        return True

    async def set_operator_disabled(self, username: str, disabled: bool) -> bool:
        """Disabling also ends the operator's sessions. False if unknown."""
        async with self.engine.begin() as c:
            q = sa.update(operators).where(operators.c.username == username)
            if (await c.execute(q.values(disabled=disabled))).rowcount == 0:
                return False
            if disabled:
                await c.execute(sa.delete(operator_sessions).where(operator_sessions.c.username == username))
        return True

    async def add_operator_session(self, token_hash: str, username: str, expires_at: datetime) -> None:
        async with self.engine.begin() as c:
            await c.execute(sa.delete(operator_sessions).where(operator_sessions.c.expires_at < now()))
            await c.execute(sa.update(operators).where(operators.c.username == username).values(last_login=now()))
            await c.execute(
                sa.insert(operator_sessions).values(
                    token_hash=token_hash, username=username, created_at=now(), expires_at=expires_at
                )
            )

    async def operator_for_session(self, token_hash: str) -> dict[str, Any] | None:
        """``{username, display_name, expires_at}`` of a live session of an enabled operator, else None."""
        q = (
            sa.select(operators.c.username, operators.c.display_name, operator_sessions.c.expires_at)
            .select_from(operator_sessions.join(operators, operators.c.username == operator_sessions.c.username))
            .where(operator_sessions.c.token_hash == token_hash, operators.c.disabled.is_(False))
        )
        async with self.engine.connect() as c:
            row = (await c.execute(q)).mappings().first()
        if row is None:
            return None
        expires = row["expires_at"] if row["expires_at"].tzinfo else row["expires_at"].replace(tzinfo=UTC)
        if expires <= now():
            return None
        return {"username": row["username"], "display_name": row["display_name"], "expires_at": expires}

    async def end_operator_session(self, token_hash: str) -> None:
        async with self.engine.begin() as c:
            await c.execute(sa.delete(operator_sessions).where(operator_sessions.c.token_hash == token_hash))

    # --- route requests (idempotent on request_id) ----------------------------------
    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as c:
            q = sa.select(route_requests).where(route_requests.c.request_id == request_id)
            row = (await c.execute(q)).mappings().first()
        return dict(row) if row else None

    async def latest_request(self, aircraft_id: str) -> dict[str, Any] | None:
        q = (
            sa.select(route_requests)
            .where(route_requests.c.aircraft_id == aircraft_id)
            .order_by(route_requests.c.created_at.desc(), route_requests.c.revision.desc())
            .limit(1)
        )
        async with self.engine.connect() as c:
            row = (await c.execute(q)).mappings().first()
        return dict(row) if row else None

    async def save_request(self, **values: Any) -> bool:
        """False if this request_id was already stored (a duplicate delivery)."""
        try:
            async with self.engine.begin() as c:
                await c.execute(sa.insert(route_requests).values(created_at=now(), **values))
            return True
        except IntegrityError:
            return False

    # --- route-response revisions ------------------------------------------------------
    async def add_revision(self, request_id: str, revision: int, origin: str | None, response: dict[str, Any]) -> None:
        async with self.engine.begin() as c:
            await c.execute(
                sa.insert(route_revisions).values(
                    request_id=request_id, revision=revision, origin=origin, response=response, created_at=now()
                )
            )

    async def revisions(self, request_id: str) -> list[dict[str, Any]]:
        """Every stored revision of a request, oldest first."""
        q = sa.select(route_revisions).where(route_revisions.c.request_id == request_id).order_by(route_revisions.c.id)
        async with self.engine.connect() as c:
            return [dict(r) for r in (await c.execute(q)).mappings().all()]

    async def max_revision(self, request_id: str) -> int:
        """Highest revision number sent for a request (0 when only the first response exists)."""
        q = sa.select(sa.func.max(route_revisions.c.revision)).where(route_revisions.c.request_id == request_id)
        async with self.engine.connect() as c:
            return int((await c.execute(q)).scalar() or 0)

    # --- planned flights --------------------------------------------------------------
    async def add_flight(self, **values: Any) -> None:
        async with self.engine.begin() as c:
            await c.execute(sa.insert(planned_flights).values(updated_at=now(), **values))

    async def set_flight_status(self, flight_id: str, status: str) -> None:
        async with self.engine.begin() as c:
            q = sa.update(planned_flights).where(planned_flights.c.flight_id == flight_id)
            await c.execute(q.values(status=status, updated_at=now()))

    async def flights(self, active_only: bool = False) -> list[dict[str, Any]]:
        q = sa.select(planned_flights).order_by(planned_flights.c.updated_at)
        if active_only:
            q = q.where(planned_flights.c.status.in_(ACTIVE_STATUSES))
        async with self.engine.connect() as c:
            return [dict(r) for r in (await c.execute(q)).mappings().all()]

    # --- telemetry ---------------------------------------------------------------------
    async def add_telemetry(self, rows: list[dict[str, Any]]) -> None:
        if rows:
            async with self.engine.begin() as c:
                await c.execute(sa.insert(telemetry), rows)

    async def telemetry_history(self, aircraft_id: str, limit: int = 600) -> list[dict[str, Any]]:
        q = (
            sa.select(telemetry)
            .where(telemetry.c.aircraft_id == aircraft_id)
            .order_by(telemetry.c.timestamp.desc())
            .limit(limit)
        )
        async with self.engine.connect() as c:
            rows = (await c.execute(q)).mappings().all()
        return [dict(r) for r in reversed(rows)]

    # --- event log (E6.9) --------------------------------------------------------------
    async def add_event(self, **values: Any) -> int:
        async with self.engine.begin() as c:
            res = await c.execute(sa.insert(events).values(**values))
            pk = res.inserted_primary_key
            assert pk is not None  # an INSERT always returns its key
            return int(pk[0])

    async def get_event(self, event_id: int) -> dict[str, Any] | None:
        async with self.engine.connect() as c:
            row = (await c.execute(sa.select(events).where(events.c.id == event_id))).mappings().first()
        return dict(row) if row else None

    async def events(
        self, since: datetime | None = None, kinds: list[str] | None = None, unacked: bool = False, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Newest first."""
        q = sa.select(events).order_by(events.c.id.desc()).limit(limit)
        if since is not None:
            q = q.where(events.c.at >= since)
        if kinds:
            q = q.where(events.c.kind.in_(kinds))
        if unacked:
            q = q.where(events.c.acked_at.is_(None))
        async with self.engine.connect() as c:
            return [dict(r) for r in (await c.execute(q)).mappings().all()]

    async def ack_event(self, event_id: int, operator: str, at: datetime) -> bool:
        """Acknowledge once; True if this call did it (a second acknowledgement keeps the first)."""
        q = (
            sa.update(events)
            .where(events.c.id == event_id, events.c.acked_at.is_(None))
            .values(acked_at=at, acked_by=operator)
        )
        async with self.engine.begin() as c:
            return (await c.execute(q)).rowcount == 1

    # --- ATC clearance marks (E6.14) ----------------------------------------------------
    async def get_clearances(self) -> dict[str, dict[str, Any]]:
        """Every mark, by flight id."""
        async with self.engine.connect() as c:
            rows = (await c.execute(sa.select(clearances))).mappings().all()
        return {r["flight_id"]: dict(r) for r in rows}

    async def set_clearance(self, flight_id: str, requirement: str, status: str, operator: str, at: datetime) -> None:
        async with self.engine.begin() as c:
            await c.execute(sa.delete(clearances).where(clearances.c.flight_id == flight_id))
            await c.execute(
                sa.insert(clearances).values(
                    flight_id=flight_id, requirement=requirement, status=status, operator=operator, at=at
                )
            )

    async def delete_clearance(self, flight_id: str) -> None:
        async with self.engine.begin() as c:
            await c.execute(sa.delete(clearances).where(clearances.c.flight_id == flight_id))

    # --- AI reroute proposals (U1) -----------------------------------------------------
    async def add_reroute(self, row: dict[str, Any]) -> None:
        async with self.engine.begin() as c:
            await c.execute(sa.insert(reroutes).values(**row))

    async def get_reroute(self, reroute_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as c:
            row = (await c.execute(sa.select(reroutes).where(reroutes.c.id == reroute_id))).mappings().first()
        return dict(row) if row else None

    async def reroutes_where(
        self, statuses: list[str] | None = None, flight_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Proposals by status (all when None), oldest first; optionally of one flight."""
        q = sa.select(reroutes).order_by(reroutes.c.created_at, reroutes.c.id)
        if statuses:
            q = q.where(reroutes.c.status.in_(statuses))
        if flight_id is not None:
            q = q.where(reroutes.c.flight_id == flight_id)
        async with self.engine.connect() as c:
            return [dict(r) for r in (await c.execute(q)).mappings().all()]

    async def update_reroute(self, reroute_id: str, **values: Any) -> None:
        async with self.engine.begin() as c:
            await c.execute(sa.update(reroutes).where(reroutes.c.id == reroute_id).values(**values))

    async def write_probe(self) -> None:
        """Exercise the write path without changing anything (a read-only or full database raises)."""
        async with self.engine.begin() as c:
            await c.execute(sa.update(devices).where(sa.false()).values(online=devices.c.online))

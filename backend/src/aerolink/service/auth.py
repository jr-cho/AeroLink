"""ATC operator login (G2, ARCHITECTURE §13.4).

Operators sign in to the operations dashboard with a username and password; pilots never do (they pair a screen
to a unit by scanning its QR code, see ``pairing.py``). Accounts are made with ``python -m aerolink.service.operators``.

``POST /api/auth/login``  ``{username, password}`` -> 200 ``{username, name, expires_at}`` and the cookie
``aerolink_ops`` (HTTP-only, SameSite=Strict, Secure unless ``AEROLINK_COOKIE_SECURE=0``, 12 h). Any failure (unknown
user, wrong password, disabled account) is the same 401 ``invalid_login``: the answer never says which part was wrong.
Wrong attempts are limited per username (5 per 60 s) and per client address (20 per 60 s): 429 ``too_many_attempts``.
``POST /api/auth/logout``  ends the session and clears the cookie (always 200).
``GET /api/auth/me``  -> ``{username, name, expires_at}`` or 401 ``session_required``. Caddy also calls it
(``forward_auth``) to let a logged-in operator reach the read-only endpoints the dashboard shares with the web map
without the basic-auth prompt.

``require_operator`` is the dependency that guards ``/api/ops/*``, ``/api/alerts*`` and the system-control ``PUT``\\ s;
``ws_operator`` does the same for ``/ws/ops``. Only the SHA-256 of the session token is stored, so a database leak
cannot be replayed as a login.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel, ConfigDict, Field

from aerolink.service import pairing
from aerolink.service.ai_api import ack_version, record_ack

COOKIE = "aerolink_ops"
SESSION_TTL_S = 12 * 3600
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256  # scrypt would happily chew on a megabyte; nobody needs one
USER_MAX_FAILURES, IP_MAX_FAILURES = 5, 20

router = APIRouter()


def normalise_username(username: str) -> str:
    return username.strip().lower()


def check_username(username: str) -> str:
    """The normalised username, or ValueError."""
    u = normalise_username(username)
    if not USERNAME_RE.match(u):
        raise ValueError("username must be 2-32 characters: a-z, 0-9, '.', '_' or '-', starting with a letter or digit")
    return u


def check_password(password: str) -> str:
    if not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH):
        raise ValueError(f"password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters")
    return password


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, **pairing.SCRYPT)


def make_hash(password: str) -> tuple[str, str]:
    """(hash_hex, salt_hex) for storage; the salt is random per operator."""
    salt = secrets.token_bytes(16)
    return hash_password(password, salt).hex(), salt.hex()


def verify(password: str, stored_hash: str, salt_hex: str) -> bool:
    return hmac.compare_digest(hash_password(password, bytes.fromhex(salt_hex)), bytes.fromhex(stored_hash))


_DUMMY = (hash_password("-", bytes(16)).hex(), "00" * 16)  # an unknown username costs the same scrypt as a known one


def _secure_cookie() -> bool:
    return os.environ.get("AEROLINK_COOKIE_SECURE", "1").lower() not in ("0", "false", "no")


def client_address(request: Request) -> str:
    """The caller's address: Caddy puts the real one first in X-Forwarded-For (the backend is not exposed directly)."""
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "unknown")


class Limiters:
    """Failed-login limits, one per username and one per address (same sliding window as pairing)."""

    def __init__(self, clock=None) -> None:
        kw = {"clock": clock} if clock else {}
        self.user = pairing.RateLimiter(max_failures=USER_MAX_FAILURES, **kw)
        self.ip = pairing.RateLimiter(max_failures=IP_MAX_FAILURES, **kw)


def _limiters(request: Request) -> Limiters:
    st = request.app.state
    if getattr(st, "login_limiters", None) is None:
        st.login_limiters = Limiters()
    return st.login_limiters


def err(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status, {"error": code, "message": message}, headers=headers)


async def create_operator(store, username: str, password: str, name: str | None = None) -> str:
    """Validate and store a new operator; returns the normalised username. ValueError on bad input or a taken name."""
    u = check_username(username)
    check_password(password)
    pw_hash, salt = await asyncio.to_thread(make_hash, password)
    if not await store.add_operator(u, (name or u).strip()[:64] or u, pw_hash, salt):
        raise ValueError(f"operator {u!r} already exists")
    return u


async def session_operator(store, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    return await store.operator_for_session(pairing.token_hash(token))


async def require_operator(request: Request) -> str:
    """FastAPI dependency: the logged-in operator's username, or 401."""
    op = await session_operator(request.app.state.service.store, request.cookies.get(COOKIE))
    if op is None:
        raise err(401, "session_required", "sign in to AeroLink Ops")
    request.state.operator = op["username"]
    return op["username"]


async def ws_operator(ws: WebSocket) -> str | None:
    """The operator behind a WebSocket's cookie (None: refuse the handshake)."""
    op = await session_operator(ws.app.state.service.store, ws.cookies.get(COOKIE))
    return op["username"] if op else None


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    username: str
    password: str


@router.post("/api/auth/login")
async def login(request: Request, response: Response, body: LoginBody) -> dict[str, Any]:
    store, lim = request.app.state.service.store, _limiters(request)
    user, ip = normalise_username(body.username)[:64], client_address(request)
    if lim.user.blocked(user) or lim.ip.blocked(ip):
        raise err(429, "too_many_attempts", "too many attempts; wait a minute", {"Retry-After": "60"})
    row = await store.get_operator(user)
    stored = (row["password_hash"], row["password_salt"]) if row else _DUMMY
    good = False
    if len(body.password) <= MAX_PASSWORD_LENGTH:  # scrypt is slow: off the event loop
        good = await asyncio.to_thread(verify, body.password, *stored)
    if not (good and row and not row["disabled"]):
        lim.user.fail(user)
        lim.ip.fail(ip)
        raise err(401, "invalid_login", "Wrong username or password")
    lim.user.reset(user)
    token = pairing.new_token()
    expires = datetime.now(UTC) + timedelta(seconds=SESSION_TTL_S)
    await store.add_operator_session(pairing.token_hash(token), user, expires)
    response.set_cookie(
        COOKIE, token, max_age=SESSION_TTL_S, httponly=True, secure=_secure_cookie(), samesite="strict", path="/"
    )
    return {"username": user, "name": row["display_name"], "expires_at": expires.isoformat()}


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    token = request.cookies.get(COOKIE)
    if token:
        await request.app.state.service.store.end_operator_session(pairing.token_hash(token))
    response.delete_cookie(COOKIE, path="/", httponly=True, secure=_secure_cookie(), samesite="strict")
    return {"ok": True}


@router.get("/api/auth/me")
async def me(request: Request) -> dict[str, Any]:
    """Who is signed in and until when (the dashboard signs out by itself a few seconds before ``expires_at``).

    ``ai_ack_version`` tells the dashboard whether this operator still has to accept the AI disclaimer (U-API §3).
    """
    store = request.app.state.service.store
    op = await session_operator(store, request.cookies.get(COOKIE))
    if op is None:
        raise err(401, "session_required", "sign in to AeroLink Ops")
    return {
        "username": op["username"],
        "name": op["display_name"],
        "expires_at": op["expires_at"].isoformat(),
        "ai_ack_version": await ack_version(store, operator=op["username"]),
    }


class AiAckBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: str = Field(min_length=1, max_length=32)


@router.post("/api/auth/ai-ack", status_code=204)
async def ai_ack(request: Request, body: AiAckBody, operator: str = Depends(require_operator)) -> Response:
    """The operator accepts the AI advisory (recorded as an ``ai_ack`` event)."""
    await record_ack(request.app.state.service, body.version, by=operator)
    return Response(status_code=204)

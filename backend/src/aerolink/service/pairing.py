"""Pairing codes and screen sessions (ARCHITECTURE §14.5).

* A unit's pairing code is 10 base32 characters (50 bits), typeable and unambiguous. Only a
  scrypt hash with a per-device salt is stored; the code is shown once, at provisioning.
* A paired screen gets a random session token. Only its SHA-256 is stored.
* Wrong codes are compared in constant time and rate limited per device.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

CODE_LENGTH = 10
SESSION_TTL_S = 7 * 24 * 3600
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}
MAX_FAILURES = 5
FAILURE_WINDOW_S = 60.0


def new_code() -> str:
    """10 random base32 characters, e.g. ``K7QF2XM4TB`` (no padding, upper case)."""
    return base64.b32encode(secrets.token_bytes(7))[:CODE_LENGTH].decode()


def normalise(code: str) -> str:
    """Forgive case, spaces and dashes when a pilot types the code by hand."""
    return code.strip().replace("-", "").replace(" ", "").upper()


def hash_code(code: str, salt: bytes) -> bytes:
    return hashlib.scrypt(normalise(code).encode(), salt=salt, **SCRYPT)


def make_hash(code: str) -> tuple[str, str]:
    """(hash_hex, salt_hex) for storage."""
    salt = secrets.token_bytes(16)
    return hash_code(code, salt).hex(), salt.hex()


def verify(code: str, stored_hash: str, salt_hex: str) -> bool:
    return hmac.compare_digest(hash_code(code, bytes.fromhex(salt_hex)), bytes.fromhex(stored_hash))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RateLimiter:
    """At most MAX_FAILURES wrong codes per device per FAILURE_WINDOW_S; then all attempts wait."""

    def __init__(self, clock=time.monotonic, max_failures: int = MAX_FAILURES, window_s: float = FAILURE_WINDOW_S):
        self.clock, self.max_failures, self.window_s = clock, max_failures, window_s
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, device_id: str) -> deque[float]:
        q, now = self._failures[device_id], self.clock()
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def blocked(self, device_id: str) -> bool:
        return len(self._prune(device_id)) >= self.max_failures

    def fail(self, device_id: str) -> None:
        self._prune(device_id).append(self.clock())

    def reset(self, device_id: str) -> None:
        self._failures.pop(device_id, None)

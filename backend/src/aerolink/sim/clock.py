"""Simulated clock for deterministic offline simulation (PLAN E3.2).

Time is derived from an integer step count, so it never drifts the way
repeated ``t += dt`` float addition does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class SimClock:
    """Immutable fixed-step clock. ``tick()`` returns a new clock."""

    start: datetime
    dt_s: float
    step: int = 0

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise ValueError("SimClock.start must be timezone-aware")
        if not self.dt_s > 0:
            raise ValueError(f"dt_s must be positive, got {self.dt_s}")
        if self.step < 0:
            raise ValueError(f"step must be non-negative, got {self.step}")

    @classmethod
    def from_iso(cls, iso: str, dt_s: float) -> SimClock:
        """Parse an ISO-8601 start time. Naive times are treated as UTC."""
        start = datetime.fromisoformat(iso)
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        return cls(start=start, dt_s=dt_s)

    @property
    def t(self) -> float:
        """Seconds elapsed since start."""
        return self.step * self.dt_s

    def now(self) -> datetime:
        """Current time in UTC."""
        return (self.start + timedelta(seconds=self.t)).astimezone(UTC)

    def local_hour(self) -> float:
        """Fractional hour of day in the start time's own timezone (0 <= h < 24)."""
        local = self.start + timedelta(seconds=self.t)
        return (local.hour + local.minute / 60.0 + (local.second + local.microsecond / 1e6) / 3600.0) % 24.0

    def tick(self) -> SimClock:
        return SimClock(start=self.start, dt_s=self.dt_s, step=self.step + 1)

"""Synthetic demand: diurnal passenger trips and medical dispatches (PLAN E3.2).

Demand data is synthetic (ARCHITECTURE §10): passenger trip requests follow a
non-homogeneous Poisson process with morning and evening peaks; medical
dispatches are a homogeneous Poisson process.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np

from aerolink.network import Network
from aerolink.sim.state import AircraftState, WorldState


@dataclass(frozen=True)
class ScheduleEntry:
    """One scheduled flight leg.

    ``aircraft`` may be an aircraft id (sim-p01) or a vehicle type
    (passenger/medical).  ``departure_local`` is an ISO time-of-day string
    ``HH:MM:SS``.  ``repeat_every_s`` > 0 turns the entry into a repeating
    shuttle.
    """

    aircraft: str
    origin: str
    destination: str
    departure_local: str
    purpose: str = "passenger"
    repeat_every_s: float | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", self.departure_local):
            raise ValueError("departure_local must be an ISO time-of-day HH:MM:SS")
        if self.repeat_every_s is not None and self.repeat_every_s <= 0:
            raise ValueError("repeat_every_s must be positive")

    @property
    def departure_seconds(self) -> float:
        return _time_to_seconds(self.departure_local)

    def matches_aircraft(self, ac: AircraftState) -> bool:
        return self.aircraft == ac.aircraft_id or self.aircraft == ac.vehicle_type


def _time_to_seconds(t: str) -> float:
    h, m, s = map(int, t.split(":"))
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        raise ValueError(f"invalid time {t}")
    return h * 3600.0 + m * 60.0 + s


def _seconds_to_time(seconds: float) -> str:
    seconds = seconds % 86400.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_schedule(raw: list[dict]) -> list[ScheduleEntry]:
    out: list[ScheduleEntry] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"schedule entry {i} must be a mapping")
        required = {"aircraft", "origin", "destination", "departure_local"}
        missing = required - set(item)
        if missing:
            raise ValueError(f"schedule entry {i} missing {sorted(missing)}")
        unknown = set(item) - required - {"purpose", "repeat_every_s"}
        if unknown:
            raise ValueError(f"schedule entry {i} unknown keys {sorted(unknown)}")
        aircraft = item["aircraft"]
        if not isinstance(aircraft, str):
            raise ValueError(f"schedule entry {i} aircraft must be a string")
        origin = item["origin"]
        destination = item["destination"]
        if not isinstance(origin, str) or not isinstance(destination, str):
            raise ValueError(f"schedule entry {i} origin/destination must be strings")
        repeat = item.get("repeat_every_s")
        if repeat is not None and not isinstance(repeat, (int, float)):
            raise ValueError(f"schedule entry {i} repeat_every_s must be a number")
        out.append(
            ScheduleEntry(
                aircraft=aircraft,
                origin=origin,
                destination=destination,
                departure_local=item["departure_local"],
                purpose=item.get("purpose", "passenger"),
                repeat_every_s=repeat,
            )
        )
    return out


# --------------------------------------------------------------------------- demand config / helpers


@dataclass(frozen=True)
class DemandConfig:
    peak_rate_per_hour: float = 1.5  # trip requests per passenger aircraft per hour at peak
    off_peak_rate_per_hour: float = 0.3
    morning_peak_hour: float = 8.0  # local time
    evening_peak_hour: float = 18.0
    peak_width_h: float = 1.5  # Gaussian sigma of each peak
    return_home_probability: float = 0.6
    medical_dispatch_mean_interval_s: float = 1800.0  # <= 0 disables dispatches
    # A dispatch no aircraft has taken after this long is dropped as
    # ``dispatch_expired`` (another service took the call). None: wait forever.
    medical_dispatch_timeout_s: float | None = None

    def __post_init__(self) -> None:
        if self.peak_rate_per_hour < 0 or self.off_peak_rate_per_hour < 0:
            raise ValueError("demand rates must be non-negative")
        if self.peak_rate_per_hour < self.off_peak_rate_per_hour:
            raise ValueError("peak_rate_per_hour must be >= off_peak_rate_per_hour")
        if self.peak_width_h <= 0:
            raise ValueError("peak_width_h must be positive")
        if not 0.0 <= self.return_home_probability <= 1.0:
            raise ValueError("return_home_probability must be in [0, 1]")
        if self.medical_dispatch_timeout_s is not None and self.medical_dispatch_timeout_s <= 0:
            raise ValueError("medical_dispatch_timeout_s must be positive or null")


def _hour_distance(a: float, b: float) -> float:
    d = abs(a - b) % 24.0
    return min(d, 24.0 - d)


def diurnal_rate_per_hour(local_hour: float, cfg: DemandConfig) -> float:
    """Trip-request rate at a local hour of day; bounded by [off_peak, peak]."""
    bump = max(
        math.exp(-0.5 * (_hour_distance(local_hour, cfg.morning_peak_hour) / cfg.peak_width_h) ** 2),
        math.exp(-0.5 * (_hour_distance(local_hour, cfg.evening_peak_hour) / cfg.peak_width_h) ** 2),
    )
    return cfg.off_peak_rate_per_hour + (cfg.peak_rate_per_hour - cfg.off_peak_rate_per_hour) * bump


def sample_next_trip_s(
    t_s: float,
    start_local_hour: float,
    rng: np.random.Generator,
    cfg: DemandConfig,
    horizon_s: float = 7 * 86_400.0,
) -> float | None:
    """Next request time after ``t_s`` by thinning (Lewis–Shedler).

    ``start_local_hour`` is the local hour of day at ``t_s == 0``. Returns None if
    no request occurs within ``horizon_s`` (e.g. all rates zero).
    """
    lam_max = cfg.peak_rate_per_hour / 3600.0
    if lam_max <= 0:
        return None
    t = t_s
    while True:
        t += float(rng.exponential(1.0 / lam_max))
        if t - t_s > horizon_s:
            return None
        hour = (start_local_hour + t / 3600.0) % 24.0
        if rng.random() * lam_max <= diurnal_rate_per_hour(hour, cfg) / 3600.0:
            return t


def next_medical_dispatch_s(t_s: float, rng: np.random.Generator, mean_interval_s: float) -> float | None:
    if mean_interval_s <= 0:
        return None
    return t_s + float(rng.exponential(mean_interval_s))


def choose_incident_site(world: WorldState, network: Network, rng: np.random.Generator) -> str | None:
    """A medical site in the fleet's service area (some medical aircraft can fly
    home -> site -> home) that has a free pad slot and is not where an idle
    medical aircraft is already parked (that case needs no flight)."""
    idle_at = {a.current_node for a in world.aircraft.values() if a.vehicle_type == "medical" and a.status == "parked"}
    candidates = [
        n
        for n in sorted(network.nodes_for("medical"))
        if n not in idle_at and world.pads[n].has_free_slot() and in_service_area(network, world.aircraft, n)
    ]
    if not candidates:
        return None
    return candidates[int(rng.integers(len(candidates)))]


def find_nearest_idle_medical_aircraft(world: WorldState, node_id: str, network: Network) -> str | None:
    """The parked, unassigned medical aircraft with the shortest route to ``node_id``.

    Ties break on aircraft id. Aircraft that cannot reach ``node_id`` within
    range, or could not get home from it, are skipped.
    """
    best: tuple[float, str] | None = None
    for aid in sorted(world.aircraft):
        ac = world.aircraft[aid]
        if ac.vehicle_type != "medical" or ac.status != "parked" or ac.active_flight is not None:
            continue
        if ac.current_node is None:
            continue
        if not can_serve_incident(network, ac, node_id):
            continue
        dist = network.route_distance_m(ac.current_node, node_id, "medical")
        assert dist is not None
        if best is None or dist < best[0]:
            best = (dist, aid)
    return best[1] if best else None


# --------------------------------------------------------------------------- reach helpers used above

from aerolink.sim.reach import can_serve_incident, in_service_area  # noqa: E402

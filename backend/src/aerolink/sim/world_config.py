"""World toggles and operator configuration (PLAN E3.3).

All toggles off gives exactly the E3.2 behaviour (verified by golden digest).
"""

from __future__ import annotations

from dataclasses import dataclass

# Behaviours planned in E3.3 but not implemented yet. Each story removes its
# toggle from this set when it lands.
NOT_BUILT: frozenset[str] = frozenset()


@dataclass(frozen=True)
class WorldToggles:
    """Operator/pilot behaviours that can be switched on or off per scenario.

    The default is all off, which reproduces the E3.2 simulator exactly.
    Toggles for behaviours that are not built yet are rejected, so a scenario
    can never claim a behaviour the simulator does not run.
    """

    separation: bool = False
    energy: bool = False
    pad_booking: bool = False
    arrival_metering: bool = False
    schedules: bool = False
    background_traffic: bool = False

    def __post_init__(self) -> None:
        on = sorted(name for name in NOT_BUILT if getattr(self, name))
        if on:
            raise ValueError(f"world toggles not implemented yet: {on}")
        # metering implies pad_booking; pad_booking implies separation
        if self.arrival_metering and not self.pad_booking:
            raise ValueError("arrival_metering requires pad_booking")
        if self.pad_booking and not self.separation:
            raise ValueError("pad_booking requires separation")


@dataclass(frozen=True)
class SeparationConfig:
    follow_target_distance_m: float = 600.0
    min_separation_m: float = 300.0
    min_vertical_separation_m: float = 150.0
    follow_lookahead_m: float = 2000.0  # leader search distance, across leg boundaries
    follow_time_constant_s: float = 30.0  # gap error closed per second = error / this
    junction_gap_s: float = 20.0
    junction_horizon_s: float = 120.0
    stack_distance_m: float = 400.0  # spacing between aircraft holding for the same pad
    terminal_radius_m: float = 3000.0  # departure release looks for traffic this close to the site
    release_gap_s: float = 60.0  # no departure this soon before a cleared arrival lands
    pad_area_m: float = 300.0  # pad sequencing area: pairs both inside it are not a loss of separation
    spacing_same_edge_m: float = 1000.0
    spacing_junction_s: float = 60.0

    def __post_init__(self) -> None:
        if not 0 < self.min_separation_m <= self.follow_target_distance_m:
            raise ValueError("min_separation_m must be positive and <= follow_target_distance_m")
        if self.min_vertical_separation_m <= 0:
            raise ValueError("min_vertical_separation_m must be positive")
        if self.junction_gap_s <= 0:
            raise ValueError("junction_gap_s must be positive")
        if self.spacing_same_edge_m <= 0 or self.spacing_junction_s <= 0:
            raise ValueError("spacing parameters must be positive")
        for name in (
            "follow_lookahead_m",
            "follow_time_constant_s",
            "junction_horizon_s",
            "stack_distance_m",
            "terminal_radius_m",
            "pad_area_m",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.release_gap_s < 0:
            raise ValueError("release_gap_s must be non-negative")


@dataclass(frozen=True)
class EnergyConfig:
    reserve_soc: float = 0.20
    charge_rate_soc_per_s: float = 0.001  # full charge in ~17 minutes of turnaround
    headwind_penalty_factor: float = 0.5  # extra cruise energy per headwind mps

    def __post_init__(self) -> None:
        if not 0.0 < self.reserve_soc < 1.0:
            raise ValueError("reserve_soc must be in (0, 1)")
        if self.charge_rate_soc_per_s <= 0:
            raise ValueError("charge_rate_soc_per_s must be positive")
        if self.headwind_penalty_factor < 0:
            raise ValueError("headwind_penalty_factor must be non-negative")


@dataclass(frozen=True)
class BackgroundTrafficConfig:
    """Synthetic non-cooperative traffic generator (E3.3 T6)."""

    count: int = 0
    start_time_s: float = 0.0
    duration_s: float = 3600.0
    altitude_msl_m: float = 150.0
    speed_mps: float = 50.0
    speed_sigma: float = 10.0
    seed_offset: int = 12345  # deterministic for benchmarking; change for randomness
    # Real-data replay (PLAN E5.1): a segments Parquet from backend/data/adsblol/. When set,
    # real flights on the corridors replace the synthetic tracks; `count` > 0 caps how many.
    replay_file: str = ""

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("count must be non-negative")
        if self.duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if self.speed_mps <= 0:
            raise ValueError("speed_mps must be positive")
        if self.speed_sigma < 0:
            raise ValueError("speed_sigma must be non-negative")
        if self.altitude_msl_m < 0:
            raise ValueError("altitude_msl_m must be non-negative")


@dataclass(frozen=True)
class PadBookingConfig:
    slot_tolerance_s: float = 120.0
    medical_can_take_passenger_slot: bool = True

    def __post_init__(self) -> None:
        if self.slot_tolerance_s < 0:
            raise ValueError("slot_tolerance_s must be non-negative")

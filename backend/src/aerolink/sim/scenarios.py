"""Scenario files (PLAN E3.2 loader; E3.4 adds the scenario set).

A scenario YAML is a mapping whose keys are the ``Scenario`` fields; nested
``demand``, ``wind`` and ``telemetry_noise`` mappings map onto their dataclasses.
Unknown keys and out-of-range values are rejected so typos fail loudly.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aerolink.sim.demand import DemandConfig, _time_to_seconds, parse_schedule
from aerolink.sim.pads import DEFAULT_TURNAROUND_S
from aerolink.sim.telemetry import TelemetryNoise
from aerolink.sim.world_config import (
    BackgroundTrafficConfig,
    EnergyConfig,
    PadBookingConfig,
    SeparationConfig,
    WorldToggles,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_NETWORK = REPO_ROOT / "airspace" / "central-fl-v1.geojson"
SCENARIO_DIR = REPO_ROOT / "backend" / "scenarios"


@dataclass(frozen=True)
class WindConfig:
    speed_mps: float = 5.0
    direction_deg: float = 270.0
    gust_fraction: float = 0.2

    def __post_init__(self) -> None:
        if self.speed_mps < 0:
            raise ValueError("wind speed_mps must be non-negative")
        if not 0.0 <= self.gust_fraction <= 1.0:
            raise ValueError("wind gust_fraction must be in [0, 1]")


@dataclass(frozen=True)
class Scenario:
    name: str = "default"
    seed: int = 42
    start_time: str = "2026-11-20T07:00:00-05:00"  # local time drives the diurnal demand
    duration_s: float = 3600.0
    dt_s: float = 1.0
    fleet: dict[str, int] = field(default_factory=lambda: {"passenger": 7, "medical": 3})
    fleet_staging: dict[str, int] = field(default_factory=dict)  # {site: passenger aircraft based there at t=0}
    network_file: str | None = None  # relative to the repo root; default central-fl-v1
    pad_turnaround_time_s: float = DEFAULT_TURNAROUND_S
    pad_capacity: dict[str, int] = field(default_factory=dict)
    stand_capacity: dict[str, int] = field(default_factory=dict)  # default: pads (eVTOL), 1 (helipad)
    max_hold_s: float = 600.0  # holding longer than this triggers a diversion
    suggestion_acceptance_rate: float = 1.0  # ARCHITECTURE §9.1: default 100%
    destination_change_probability: float = 0.02  # per flight
    cruise_speed_sigma: float = 0.03  # per-leg fractional cruise-speed noise
    route_options: int = 3
    demand: DemandConfig = field(default_factory=DemandConfig)
    wind: WindConfig = field(default_factory=WindConfig)
    telemetry_noise: TelemetryNoise = field(default_factory=TelemetryNoise)
    schedule: list[dict[str, Any]] = field(default_factory=list)
    # Scripted medical incidents: [{at_local: "HH:MM:SS", node: "<helipad id>"}], each raised once at that
    # local time. For scenarios that need an emergency at a known place and time.
    medical_incidents: list[dict[str, str]] = field(default_factory=list)
    world: WorldToggles = field(default_factory=WorldToggles)
    separation: SeparationConfig = field(default_factory=SeparationConfig)
    energy: EnergyConfig = field(default_factory=EnergyConfig)
    pad_booking: PadBookingConfig = field(default_factory=PadBookingConfig)
    background_traffic: BackgroundTrafficConfig = field(default_factory=BackgroundTrafficConfig)

    def __post_init__(self) -> None:
        if self.duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if self.dt_s <= 0:
            raise ValueError("dt_s must be positive")
        if self.pad_turnaround_time_s < 0:
            raise ValueError("pad_turnaround_time_s must be non-negative")
        if self.max_hold_s <= 0:
            raise ValueError("max_hold_s must be positive")
        for name in ("suggestion_acceptance_rate", "destination_change_probability"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if not 0.0 <= self.cruise_speed_sigma < 0.5:
            raise ValueError("cruise_speed_sigma must be in [0, 0.5)")
        if self.route_options < 1:
            raise ValueError("route_options must be >= 1")
        unknown = set(self.fleet) - {"passenger", "medical"}
        if unknown:
            raise ValueError(f"unknown fleet vehicle types: {sorted(unknown)}")
        if any(v < 0 for v in self.fleet.values()):
            raise ValueError("fleet counts must be non-negative")
        for i, item in enumerate(self.medical_incidents):
            if not isinstance(item, dict) or set(item) != {"at_local", "node"}:
                raise ValueError(f"medical_incidents[{i}] must have exactly the keys at_local and node")
            _time_to_seconds(item["at_local"])  # raises on a bad HH:MM:SS
        if any(v < 0 for v in self.fleet_staging.values()):
            raise ValueError("fleet_staging counts must be non-negative")
        if sum(self.fleet_staging.values()) > self.fleet.get("passenger", 0):
            raise ValueError("fleet_staging places more aircraft than the passenger fleet has")

    @property
    def network_path(self) -> Path:
        if self.network_file is None:
            return DEFAULT_NETWORK
        p = Path(self.network_file)
        return p if p.is_absolute() else REPO_ROOT / p

    def with_overrides(self, **overrides: Any) -> Scenario:
        return dataclasses.replace(self, **overrides)


_NESTED = {
    "demand": DemandConfig,
    "wind": WindConfig,
    "telemetry_noise": TelemetryNoise,
    "world": WorldToggles,
    "separation": SeparationConfig,
    "energy": EnergyConfig,
    "pad_booking": PadBookingConfig,
    "background_traffic": BackgroundTrafficConfig,
}


def default_scenario() -> Scenario:
    return Scenario()


def scenario_from_dict(data: dict[str, Any], default_name: str = "default") -> Scenario:
    if not isinstance(data, dict):
        raise ValueError("scenario must be a mapping")
    known = {f.name for f in dataclasses.fields(Scenario)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown scenario keys: {sorted(unknown)}")
    kwargs: dict[str, Any] = dict(data)
    kwargs.setdefault("name", default_name)
    for key, cls in _NESTED.items():
        if key in kwargs:
            sub = kwargs[key] or {}
            sub_known = {f.name for f in dataclasses.fields(cls)}
            bad = set(sub) - sub_known
            if bad:
                raise ValueError(f"unknown {key} keys: {sorted(bad)}")
            kwargs[key] = cls(**sub)
    if "schedule" in kwargs:
        kwargs["schedule"] = parse_schedule(kwargs["schedule"] or [])
    if "fleet" in kwargs:
        kwargs["fleet"] = {"passenger": 0, "medical": 0, **kwargs["fleet"]}
    return Scenario(**kwargs)


def scenario_to_dict(scenario: Scenario) -> dict[str, Any]:
    return dataclasses.asdict(scenario)


def load_scenario(path: Path) -> Scenario:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return scenario_from_dict(data, default_name=Path(path).stem)


def save_scenario(scenario: Scenario, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(scenario_to_dict(scenario), sort_keys=False))

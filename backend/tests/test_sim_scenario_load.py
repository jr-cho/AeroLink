from pathlib import Path

import pytest

from aerolink.sim.demand import DemandConfig
from aerolink.sim.scenarios import (
    DEFAULT_NETWORK,
    SCENARIO_DIR,
    Scenario,
    WindConfig,
    default_scenario,
    load_scenario,
    save_scenario,
    scenario_from_dict,
)
from aerolink.sim.telemetry import TelemetryNoise


def test_default_network_and_dir_exist():
    assert DEFAULT_NETWORK.exists()
    assert SCENARIO_DIR.is_dir()


def test_default_scenario_values():
    s = default_scenario()
    assert s.fleet == {"passenger": 7, "medical": 3}
    assert s.suggestion_acceptance_rate == 1.0
    assert s.network_path == DEFAULT_NETWORK


@pytest.mark.parametrize("path", sorted(SCENARIO_DIR.glob("*.yaml")), ids=lambda p: p.name)
def test_every_committed_scenario_loads(path: Path):
    s = load_scenario(path)
    assert s.network_path.exists()


def test_nominal_day_matches_file():
    s = load_scenario(SCENARIO_DIR / "nominal_day.yaml")
    assert s.name == "nominal_day" and s.seed == 42 and s.duration_s == 3600
    assert isinstance(s.demand, DemandConfig) and isinstance(s.wind, WindConfig)
    assert isinstance(s.telemetry_noise, TelemetryNoise)


def test_round_trip(tmp_path: Path):
    s = default_scenario().with_overrides(name="rt", seed=7, pad_capacity={"ev-orl": 3})
    p = tmp_path / "rt.yaml"
    save_scenario(s, p)
    assert load_scenario(p) == s


def test_name_defaults_to_file_stem(tmp_path: Path):
    p = tmp_path / "my_case.yaml"
    p.write_text("seed: 3\n")
    assert load_scenario(p).name == "my_case"


def test_empty_file_is_default(tmp_path: Path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    s = load_scenario(p)
    assert s.with_overrides(name="default") == default_scenario()


def test_partial_fleet_fills_zero():
    assert scenario_from_dict({"fleet": {"medical": 2}}).fleet == {"passenger": 0, "medical": 2}


def test_null_nested_section_is_default():
    assert scenario_from_dict({"wind": None}).wind == WindConfig()


@pytest.mark.parametrize(
    "data",
    [
        {"bogus": 1},
        {"wind": {"spead_mps": 3}},
        {"demand": {"peak": 1}},
        {"telemetry_noise": {"dropout": 0.1}},
        {"duration_s": 0},
        {"dt_s": -1},
        {"pad_turnaround_time_s": -1},
        {"max_hold_s": 0},
        {"suggestion_acceptance_rate": 1.2},
        {"destination_change_probability": -0.1},
        {"cruise_speed_sigma": 0.6},
        {"route_options": 0},
        {"fleet": {"cargo": 1}},
        {"fleet": {"passenger": -1}},
        {"wind": {"speed_mps": -1}},
        {"wind": {"gust_fraction": 2}},
        {"telemetry_noise": {"dropout_rate": 2}},
        {"demand": {"peak_width_h": 0}},
    ],
)
def test_invalid_scenarios_rejected(data):
    with pytest.raises((ValueError, TypeError)):
        scenario_from_dict(data)


def test_non_mapping_rejected(tmp_path: Path):
    p = tmp_path / "list.yaml"
    p.write_text("- 1\n- 2\n")
    with pytest.raises(ValueError):
        load_scenario(p)


def test_relative_and_absolute_network_paths(tmp_path: Path):
    assert Scenario(network_file="airspace/central-fl-v1.geojson").network_path == DEFAULT_NETWORK
    assert Scenario(network_file=str(tmp_path / "x.geojson")).network_path == tmp_path / "x.geojson"


def test_scenario_is_immutable():
    with pytest.raises(AttributeError):
        default_scenario().seed = 1  # ty: ignore[invalid-assignment]

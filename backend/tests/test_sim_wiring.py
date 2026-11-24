"""Scenario settings reach the controller (hardening plan B, findings H1/M2)."""

from pathlib import Path

import pytest

from aerolink.network import Network
from aerolink.sim import load_scenario, run_simulation
from aerolink.sim import runner as runner_mod
from aerolink.sim.controller import Controller, ControllerConfig
from aerolink.sim.demand import DemandConfig
from aerolink.sim.scenarios import Scenario, scenario_from_dict
from aerolink.sim.world_config import NOT_BUILT, SeparationConfig, WorldToggles

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"


def _capture_cfg(monkeypatch) -> list[ControllerConfig]:
    seen: list[ControllerConfig] = []

    class Recording(Controller):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen.append(self.cfg)

    monkeypatch.setattr(runner_mod, "Controller", Recording)
    return seen


def test_runner_passes_world_settings(monkeypatch, fl_network: Network):
    seen = _capture_cfg(monkeypatch)
    sep = SeparationConfig(min_separation_m=321.0)
    scenario = Scenario(duration_s=5, world=WorldToggles(separation=True), separation=sep)
    run_simulation(fl_network, scenario)
    (cfg,) = seen
    assert cfg.world == scenario.world
    assert cfg.separation == sep
    assert cfg.energy == scenario.energy
    assert cfg.pad_booking == scenario.pad_booking


def test_separation_toggle_changes_output(fl_network: Network):
    """The toggle has an observable effect, so the golden digest really tests 'off'."""
    off = run_simulation(fl_network, Scenario(duration_s=1800))
    on = run_simulation(fl_network, Scenario(duration_s=1800, world=WorldToggles(separation=True)))
    assert off.digest() != on.digest()


@pytest.mark.parametrize("name", sorted(NOT_BUILT))
def test_unbuilt_toggle_rejected(name: str):
    with pytest.raises(ValueError, match="not implemented yet"):
        scenario_from_dict({"world": {name: True}})


@pytest.mark.parametrize("path", sorted(SCENARIOS.glob("*.yaml")), ids=lambda p: p.stem)
def test_committed_scenarios_use_only_built_toggles(path: Path):
    world = load_scenario(path).world
    assert not any(getattr(world, n) for n in NOT_BUILT)


def test_operator_follows_controller_cfg(fl_network: Network):
    from aerolink.sim.clock import SimClock
    from aerolink.sim.controller import ShortestPathAdvisor
    from aerolink.sim.runner import build_world

    scenario = Scenario()
    world = build_world(fl_network, scenario, 1)
    clock = SimClock.from_iso(scenario.start_time, dt_s=1.0)
    ctl = Controller(fl_network, world, clock, ShortestPathAdvisor(fl_network), ControllerConfig())
    new = ControllerConfig(demand=DemandConfig(medical_dispatch_mean_interval_s=0))
    ctl.cfg = new
    assert ctl.operator.cfg is new
    assert all(p.cfg is new for p in ctl.pilots.values())

import numpy as np
import pytest

from aerolink.network import Network
from aerolink.sim.demand import (
    DemandConfig,
    choose_incident_site,
    diurnal_rate_per_hour,
    find_nearest_idle_medical_aircraft,
    next_medical_dispatch_s,
    sample_next_trip_s,
)
from aerolink.sim.runner import build_world
from aerolink.sim.scenarios import Scenario

CFG = DemandConfig()


def test_rate_peaks_at_rush_hours_and_is_bounded():
    hours = np.linspace(0, 24, 241)
    rates = [diurnal_rate_per_hour(h, CFG) for h in hours]
    assert min(rates) >= CFG.off_peak_rate_per_hour - 1e-12
    assert max(rates) <= CFG.peak_rate_per_hour + 1e-12
    assert diurnal_rate_per_hour(8, CFG) == pytest.approx(CFG.peak_rate_per_hour)
    assert diurnal_rate_per_hour(18, CFG) == pytest.approx(CFG.peak_rate_per_hour)
    assert diurnal_rate_per_hour(3, CFG) < diurnal_rate_per_hour(7, CFG) < diurnal_rate_per_hour(8, CFG)


def test_rate_wraps_around_midnight():
    cfg = DemandConfig(morning_peak_hour=23.5)
    assert diurnal_rate_per_hour(0.5, cfg) == pytest.approx(diurnal_rate_per_hour(22.5, cfg))


def test_sampled_trips_are_increasing_and_after_now():
    rng = np.random.default_rng(0)
    t = 0.0
    for _ in range(200):
        nt = sample_next_trip_s(t, 7.0, rng, CFG)
        assert nt is not None and nt > t
        t = nt


def test_sampled_rate_matches_diurnal_profile():
    """Count trips over many simulated days: peak hour busier than 3 am, and the
    peak-hour count matches the configured rate within statistical tolerance."""
    rng = np.random.default_rng(1)
    counts = np.zeros(24)
    t, days = 0.0, 1000
    while True:
        t = sample_next_trip_s(t, 0.0, rng, CFG)
        if t is None or t >= days * 86400:
            break
        counts[int(t / 3600) % 24] += 1
    per_hour = counts / days
    expected = [np.mean([diurnal_rate_per_hour(h + f, CFG) for f in np.linspace(0, 1, 61)]) for h in range(24)]
    for h in range(24):
        assert per_hour[h] == pytest.approx(expected[h], rel=0.15), h
    assert per_hour[8] > 3 * per_hour[2]


def test_start_hour_shifts_profile():
    """t=0 at 08:00 local is peak; t=0 at 03:00 is off-peak."""

    def first_hour_count(start):
        rng = np.random.default_rng(2)
        n = 0
        for _ in range(400):
            t = sample_next_trip_s(0.0, start, rng, CFG)
            n += t is not None and t < 3600
        return n

    assert first_hour_count(8.0) > 2 * first_hour_count(3.0)


def test_zero_demand_never_generates_trips():
    cfg = DemandConfig(peak_rate_per_hour=0.0, off_peak_rate_per_hour=0.0)
    assert sample_next_trip_s(0.0, 8.0, np.random.default_rng(0), cfg) is None


def test_horizon_limits_search():
    cfg = DemandConfig(peak_rate_per_hour=1e-6, off_peak_rate_per_hour=0.0)
    assert sample_next_trip_s(0.0, 8.0, np.random.default_rng(0), cfg, horizon_s=1.0) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"peak_rate_per_hour": -1},
        {"off_peak_rate_per_hour": -1},
        {"peak_rate_per_hour": 0.1, "off_peak_rate_per_hour": 0.5},
        {"peak_width_h": 0},
        {"return_home_probability": 1.5},
    ],
)
def test_invalid_demand_config(kwargs):
    with pytest.raises(ValueError):
        DemandConfig(**kwargs)


def test_medical_dispatch_interval():
    rng = np.random.default_rng(0)
    gaps = [next_medical_dispatch_s(0.0, rng, 1800.0) for _ in range(4000)]
    assert all(g is not None and g > 0 for g in gaps)
    assert np.mean([g for g in gaps if g is not None]) == pytest.approx(1800, rel=0.06)
    assert next_medical_dispatch_s(5.0, rng, 0) is None
    assert next_medical_dispatch_s(5.0, rng, -1) is None


def _world(net: Network, seed=0):
    return build_world(net, Scenario(), seed)


def test_incident_site_is_free_medical_site_without_idle_aircraft(fl_network: Network):
    world = _world(fl_network)
    idle = {a.current_node for a in world.aircraft.values() if a.vehicle_type == "medical"}
    rng = np.random.default_rng(0)
    for _ in range(100):
        s = choose_incident_site(world, fl_network, rng)
        assert s in fl_network.nodes_for("medical") and s not in idle
        assert world.pads[s].has_free_slot()


def test_incident_site_none_when_all_full(fl_network: Network):
    world = _world(fl_network)
    for n in fl_network.nodes_for("medical"):
        world.pads[n].occupants = ["x"] * world.pads[n].capacity
    assert choose_incident_site(world, fl_network, np.random.default_rng(0)) is None


def test_nearest_idle_medical_aircraft(fl_network: Network):
    world = _world(fl_network)
    meds = sorted(a for a, s in world.aircraft.items() if s.vehicle_type == "medical")
    target = next(
        n for n in sorted(fl_network.nodes_for("medical")) if n not in {world.aircraft[m].current_node for m in meds}
    )

    def dist(aid):
        p = fl_network.shortest_paths(world.aircraft[aid].current_node, target, vehicle_type="medical", k=1)
        return fl_network.route_length_m(p[0])

    expected = min(meds, key=lambda a: (dist(a), a))
    assert find_nearest_idle_medical_aircraft(world, target, fl_network) == expected
    # Busy aircraft are skipped.
    world.aircraft[expected].status = "cruise"
    assert find_nearest_idle_medical_aircraft(world, target, fl_network) not in (expected, None)
    for m in meds:
        world.aircraft[m].status = "turnaround"
    assert find_nearest_idle_medical_aircraft(world, target, fl_network) is None


def test_nearest_never_picks_passenger_aircraft(fl_network: Network):
    world = _world(fl_network)
    for a in world.aircraft.values():
        if a.vehicle_type == "medical":
            a.status = "cruise"
    assert find_nearest_idle_medical_aircraft(world, sorted(fl_network.nodes_for("medical"))[0], fl_network) is None

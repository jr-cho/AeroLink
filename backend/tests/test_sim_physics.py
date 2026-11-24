import math

import numpy as np
import pytest

from aerolink.network import Network
from aerolink.sim import physics as ph
from aerolink.sim.state import AircraftState, SimPosition, WindField

CALM = WindField(speed_mps=0.0, gust_fraction=0.0)


def make_ac(net: Network, path: list[str], speed: float = 60.0, vtype="passenger") -> AircraftState:
    n0 = net.nodes[path[0]]
    ac = AircraftState(
        aircraft_id="t-1",
        vehicle_type=vtype,
        home_node=path[0],
        status="climbing",
        current_node=None,
        position=SimPosition(n0["lat"], n0["lon"], ph.PAD_ALT_M),
        rng=np.random.default_rng(0),
        telemetry_rng=np.random.default_rng(1),
    )
    ac.legs = ph.build_legs(path, net, speed)
    return ac


def fly(ac, wind=CALM, dt=1.0, stop=0.0, max_steps=20_000, on_node=None, energy=None):
    trace = []
    for _ in range(max_steps):
        r = ph.advance(ac, wind, dt, stop, on_node, energy=energy)
        trace.append((ac.velocity_mps, ac.position.alt_msl_m, ac.vertical_speed_mps, r))
        if r.at_stop:
            return trace
    raise AssertionError("never reached stop")


# ---------------------------------------------------------------- primitives
def test_apply_acceleration_limits():
    assert ph.apply_acceleration(0, 100, 1) == ph.MAX_ACCEL_MPS2
    assert ph.apply_acceleration(50, 0, 1) == 50 - ph.MAX_DECEL_MPS2
    assert ph.apply_acceleration(10, 11, 1) == 11
    assert ph.apply_acceleration(10, 10, 1) == 10


def test_update_altitude_limits():
    assert ph.update_altitude(0, 1000, 1) == ph.MAX_CLIMB_MPS
    assert ph.update_altitude(1000, 0, 2) == 1000 - 2 * ph.MAX_DESCENT_MPS
    assert ph.update_altitude(100, 102, 1) == 102


def test_braking_and_glide():
    assert ph.braking_distance_m(0) == 0
    assert ph.braking_distance_m(60) == pytest.approx(1800)
    assert ph.glide_target_alt_m(800, 0) == ph.PAD_ALT_M
    assert ph.glide_target_alt_m(800, 1e9) == 800


def test_bearing_cardinal_directions():
    assert ph.bearing_deg((0, 0), (0, 1)) == pytest.approx(0)
    assert ph.bearing_deg((0, 0), (1, 0)) == pytest.approx(90)
    assert ph.bearing_deg((0, 1), (0, 0)) == pytest.approx(180)
    assert ph.bearing_deg((1, 0), (0, 0)) == pytest.approx(270)


def test_segment_length_one_degree_latitude():
    assert ph.segment_length_m((0, 0), (0, 1)) == pytest.approx(111_195, rel=1e-3)


def test_edge_cruise_alt_is_band_middle(fl_network: Network):
    e = next(iter(fl_network.edges.values()))
    alt = ph.edge_cruise_alt_m(e)
    assert e.band_floor_ft * 0.3048 < alt < e.band_ceiling_ft * 0.3048


# ------------------------------------------------------------- interpolation
def test_interpolation_endpoints_and_clamp(line_network: Network):
    e = line_network.edge_between("a", "w1")
    a, w1 = line_network.nodes["a"], line_network.nodes["w1"]
    lat, lon, crs = ph.interpolate_on_edge(e, 0, "a")
    assert (lat, lon) == pytest.approx((a["lat"], a["lon"]))
    assert crs == pytest.approx(0, abs=1e-6)
    lat, lon, _ = ph.interpolate_on_edge(e, e.length_m, "a")
    assert (lat, lon) == pytest.approx((w1["lat"], w1["lon"]))
    assert ph.interpolate_on_edge(e, -5, "a")[:2] == pytest.approx((a["lat"], a["lon"]))
    assert ph.interpolate_on_edge(e, e.length_m * 2, "a")[:2] == pytest.approx((w1["lat"], w1["lon"]))


def test_interpolation_respects_direction(line_network: Network):
    e = line_network.edge_between("a", "w1")
    w1 = line_network.nodes["w1"]
    lat, lon, crs = ph.interpolate_on_edge(e, 0, "w1")
    assert (lat, lon) == pytest.approx((w1["lat"], w1["lon"]))
    assert crs == pytest.approx(180)


def test_interpolation_reversed_edge_on_real_network(fl_network: Network):
    """Regression: travelling an edge against its stored direction used to start
    at the wrong end."""
    for e in list(set(fl_network.edges.values()))[:10]:
        tgt = fl_network.nodes[e.target]
        lat, lon, _ = ph.interpolate_on_edge(e, 0, e.target)
        assert abs(lat - tgt["lat"]) < 1e-3 and abs(lon - tgt["lon"]) < 1e-3


def test_interpolation_multi_segment_midpoint():
    from aerolink.network import Edge

    geom = ((0.0, 0.0), (0.0, 0.01), (0.01, 0.01))  # north then east
    seg = ph.segment_length_m(geom[0], geom[1]) + ph.segment_length_m(geom[1], geom[2])
    e = Edge("x", "corridor", "s", "t", seg, 1000, 1500, None, geom)
    lat, lon, crs = ph.interpolate_on_edge(e, seg * 0.25, "s")
    assert lon == pytest.approx(0) and lat == pytest.approx(0.005, rel=1e-3) and crs == pytest.approx(0, abs=0.1)
    lat, lon, crs = ph.interpolate_on_edge(e, seg * 0.75, "s")
    assert lat == pytest.approx(0.01) and crs == pytest.approx(90, abs=0.1)


def test_build_legs_orients_and_validates(line_network: Network):
    legs = ph.build_legs(["b", "w2", "w1", "a"], line_network, 50.0)
    assert [(lg.start_node, lg.end_node) for lg in legs] == [("b", "w2"), ("w2", "w1"), ("w1", "a")]
    assert all(lg.planned_speed_mps == 50 for lg in legs)
    assert [lg.planned_speed_mps for lg in ph.build_legs(["a", "w1", "w2"], line_network, [10.0, 20.0])] == [10, 20]
    with pytest.raises(ValueError):
        ph.build_legs(["a", "w1", "w2"], line_network, [10.0])


# --------------------------------------------------------------------- flight
def test_full_flight_respects_all_limits_and_stops_at_pad(line_network: Network):
    ac = make_ac(line_network, ["a", "w1", "w2", "b"])
    total = ac.remaining_route_m()
    trace = fly(ac)
    v_prev, alt_prev = 0.0, ph.PAD_ALT_M
    dist = 0.0
    for v, alt, _vs, r in trace:
        assert v - v_prev <= ph.MAX_ACCEL_MPS2 + 1e-9
        assert v_prev - v <= ph.MAX_DECEL_MPS2 + 1e-9
        assert alt - alt_prev <= ph.MAX_CLIMB_MPS + 1e-9
        assert alt_prev - alt <= ph.MAX_DESCENT_MPS + 1e-9
        assert 0 <= v <= ph.MAX_GROUND_SPEED_MPS
        v_prev, alt_prev = v, alt
        dist += r.distance_m
    assert dist == pytest.approx(total, abs=1e-6)
    assert ac.velocity_mps == 0
    b = line_network.nodes["b"]
    assert (ac.position.lat, ac.position.lon) == pytest.approx((b["lat"], b["lon"]))
    assert ac.position.alt_msl_m == pytest.approx(ph.PAD_ALT_M, abs=1.0)
    assert max(t[1] for t in trace) == pytest.approx(ac.legs[1].cruise_alt_m)
    assert {r.nodes_passed[0] for *_, r in trace if r.nodes_passed} == {"w1", "w2"}


def test_flight_reaches_cruise_speed_and_altitude(line_network: Network):
    ac = make_ac(line_network, ["a", "w1", "w2", "b"], speed=40)
    trace = fly(ac)
    assert max(t[0] for t in trace) == pytest.approx(40)
    statuses = []
    ac2 = make_ac(line_network, ["a", "w1", "w2", "b"], speed=40)
    for _ in range(len(trace)):
        ph.advance(ac2, CALM, 1.0, 0.0)
        statuses.append(ac2.status)
    assert statuses[0] == "climbing" and "cruise" in statuses and "descending" in statuses


def test_stops_at_hold_point_short_of_pad(line_network: Network):
    ac = make_ac(line_network, ["a", "w1", "w2", "b"])
    fly(ac, stop=500)
    assert ac.remaining_route_m() == pytest.approx(500, abs=1e-6)
    assert ac.velocity_mps == 0


def test_single_short_leg(line_network: Network):
    ac = make_ac(line_network, ["a", "w1"])
    trace = fly(ac)
    assert sum(t[3].distance_m for t in trace) == pytest.approx(line_network.edge_between("a", "w1").length_m)


def test_tailwind_is_faster_than_headwind(line_network: Network):
    """Route runs due north; wind 'from' south is a tailwind."""
    tail = len(fly(make_ac(line_network, ["a", "w1", "w2", "b"]), wind=WindField(10, 180, 0)))
    head = len(fly(make_ac(line_network, ["a", "w1", "w2", "b"]), wind=WindField(10, 0, 0)))
    calm = len(fly(make_ac(line_network, ["a", "w1", "w2", "b"])))
    assert tail < calm < head


def test_crosswind_has_no_along_track_effect():
    w = WindField(10, 270, 0)
    assert w.along_track_component(0) == pytest.approx(0, abs=1e-9)
    assert w.along_track_component(90) == pytest.approx(10)  # from west, flying east
    assert w.along_track_component(270) == pytest.approx(-10)


def test_headwind_stronger_than_airspeed_cannot_go_backwards(line_network: Network):
    ac = make_ac(line_network, ["a", "w1"], speed=5)
    for _ in range(50):
        r = ph.advance(ac, WindField(20, 0, 0), 1.0, 0.0)
        assert r.distance_m >= 0 and ac.velocity_mps >= 0


def test_ground_speed_capped_at_contract_max(line_network: Network):
    ac = make_ac(line_network, ["a", "w1", "w2", "b"], speed=149)
    trace = fly(ac, wind=WindField(50, 180, 0))
    assert max(t[0] for t in trace) <= ph.MAX_GROUND_SPEED_MPS


@pytest.mark.parametrize("dt", [0.25, 1.0, 2.0])
def test_different_time_steps_cover_route(line_network: Network, dt):
    ac = make_ac(line_network, ["a", "w1", "w2", "b"])
    total = ac.remaining_route_m()
    trace = fly(ac, dt=dt)
    assert sum(t[3].distance_m for t in trace) == pytest.approx(total, abs=1e-6)
    assert len(trace) * dt == pytest.approx(len(fly(make_ac(line_network, ["a", "w1", "w2", "b"]))), rel=0.05)


def test_on_node_reroute_continues_on_new_legs(line_network: Network):
    ac = make_ac(line_network, ["a", "w1", "w2", "b"])
    seen = []

    def on_node(n):
        seen.append(n)
        if n == "w2":
            return ph.build_legs(["w2", "c"], line_network, 60.0)
        return None

    fly(ac, on_node=on_node)
    c = line_network.nodes["c"]
    assert seen == ["w1", "w2"]
    assert (ac.position.lat, ac.position.lon) == pytest.approx((c["lat"], c["lon"]))


def test_battery_drains_linearly_when_energy_disabled(line_network: Network):
    ac = make_ac(line_network, ["a", "w1", "w2", "b"])
    total = ac.remaining_route_m()
    fly(ac)
    assert ac.battery_soc == pytest.approx(1 - total / 160_000)


def test_battery_drains_with_energy_model(line_network: Network):
    from aerolink.sim.world_config import EnergyConfig

    energy = EnergyConfig()
    ac = make_ac(line_network, ["a", "w1", "w2", "b"])
    total = ac.remaining_route_m()
    fly(ac, energy=energy)
    # Energy model should consume at least the cruise energy for the route,
    # plus the takeoff/climb and landing/hover transition allowance.
    assert ac.battery_soc < 1 - total / 160_000
    assert 0.0 < ac.battery_soc <= 1.0


def test_no_legs_is_immediately_at_stop(line_network: Network):
    ac = make_ac(line_network, ["a", "w1"])
    ac.legs = []
    r = ph.advance(ac, CALM, 1.0, 0.0)
    assert r.at_stop and r.distance_m == 0


def test_descent_feasible_on_glide_slope():
    """Glide slope * max approach speed must not exceed the descent limit."""
    assert ph.GLIDE_SLOPE * 60 <= ph.MAX_DESCENT_MPS
    assert math.isfinite(ph.glide_target_alt_m(800, 10))

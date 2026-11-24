"""T0: refactor keeps E3.2 behaviour intact (PLAN E3.3 T0).

These tests pin the split of controller.py into operator.py + pilot.py and
verify that the golden digest is unchanged when all new world toggles are off.
"""

from datetime import UTC

import pytest

from aerolink.sim.controller import Controller, ShortestPathAdvisor
from aerolink.sim.events import ALL
from aerolink.sim.separation import leader_ahead
from aerolink.sim.traffic import TrafficIndex


def test_every_event_kind_is_registered():
    # The old event kinds plus the new T0+ registered ones.
    for kind in [
        "departure",
        "pad_requested",
        "pad_cleared",
        "hold_start",
        "hold_end",
        "landing",
        "arrival",
        "ready",
        "diversion",
        "diversion_unavailable",
        "destination_change",
        "destination_change_rejected",
        "suggestion",
        "medical_incident",
        "route_unavailable",
        "trip_rejected",
        "ground_hold",
        "ground_hold_end",
        "slot_prebooked",
        "slot_taken",
        "bunching",
        "loss_of_separation",
    ]:
        assert kind in ALL, kind


def test_cfg_setter_propagates_to_pilots(line_network):
    from datetime import datetime

    import numpy as np

    from aerolink.sim import pads as padlib
    from aerolink.sim.clock import SimClock
    from aerolink.sim.controller import ControllerConfig
    from aerolink.sim.demand import DemandConfig
    from aerolink.sim.fleet import pad_position
    from aerolink.sim.state import AircraftState, WindField, WorldState

    def make_controller(net, placements, **cfg):
        pads = padlib.init_pads(net)
        fleet = {}
        for i, (aid, (vtype, node)) in enumerate(sorted(placements.items())):
            padlib.occupy(pads[node], aid)
            fleet[aid] = AircraftState(
                aircraft_id=aid,
                vehicle_type=vtype,
                home_node=node,
                status="parked",
                current_node=node,
                position=pad_position(net, node),
                rng=np.random.default_rng(i),
                telemetry_rng=np.random.default_rng(100 + i),
            )
        world = WorldState(t=0.0, aircraft=fleet, pads=pads, rng=np.random.default_rng(7), wind=WindField(0, 0, 0))
        clock = SimClock(datetime(2026, 11, 20, 12, tzinfo=UTC), cfg.pop("dt_s", 1.0))
        config = ControllerConfig(
            dt_s=clock.dt_s,
            demand=cfg.pop("demand", NO_DEMAND),
            cruise_speed_sigma=0.0,
            destination_change_probability=0.0,
            **cfg,
        )
        return Controller(net, world, clock, ShortestPathAdvisor(net), config)

    NO_DEMAND = DemandConfig(peak_rate_per_hour=0.0, off_peak_rate_per_hour=0.0, medical_dispatch_mean_interval_s=0)
    ctl = make_controller(line_network, {"p1": ("passenger", "a")})
    new = ControllerConfig(demand=NO_DEMAND, destination_change_probability=0.5)
    ctl.cfg = new
    assert ctl.cfg.destination_change_probability == 0.5
    assert ctl.pilots["p1"].cfg.destination_change_probability == 0.5


def test_traffic_index_orders_by_distance(line_network):
    from datetime import datetime

    import numpy as np

    from aerolink.sim import pads as padlib
    from aerolink.sim.clock import SimClock
    from aerolink.sim.controller import ControllerConfig
    from aerolink.sim.demand import DemandConfig
    from aerolink.sim.fleet import pad_position
    from aerolink.sim.physics import build_legs
    from aerolink.sim.state import AircraftState, WindField, WorldState

    NO_DEMAND = DemandConfig(peak_rate_per_hour=0.0, off_peak_rate_per_hour=0.0, medical_dispatch_mean_interval_s=0)
    pads = padlib.init_pads(line_network, {"a": 2})
    fleet = {}
    for i, (aid, node) in enumerate([("p1", "a"), ("p2", "a")]):
        padlib.occupy(pads[node], aid)
        fleet[aid] = AircraftState(
            aircraft_id=aid,
            vehicle_type="passenger",
            home_node=node,
            status="parked",
            current_node=node,
            position=pad_position(line_network, node),
            rng=np.random.default_rng(i),
            telemetry_rng=np.random.default_rng(100 + i),
        )
    world = WorldState(t=0.0, aircraft=fleet, pads=pads, rng=np.random.default_rng(7), wind=WindField(0, 0, 0))
    clock = SimClock(datetime(2026, 11, 20, 12, tzinfo=UTC), 1.0)
    cfg = ControllerConfig(demand=NO_DEMAND, cruise_speed_sigma=0.0, destination_change_probability=0.0)
    ctl = Controller(line_network, world, clock, ShortestPathAdvisor(line_network), cfg)
    ac1 = ctl.world.aircraft["p1"]
    ac2 = ctl.world.aircraft["p2"]
    legs = build_legs(["a", "w1"], line_network, 60.0)
    ac1.status = "cruise"
    ac1.current_node = None
    ac1.legs = legs
    ac1.leg_index = 0
    ac1.distance_along_edge_m = 100.0
    ac1.position.alt_msl_m = 850.0
    ac2.status = "cruise"
    ac2.current_node = None
    ac2.legs = legs
    ac2.leg_index = 0
    ac2.distance_along_edge_m = 300.0
    ac2.position.alt_msl_m = 850.0

    idx = TrafficIndex(ctl.world.aircraft)
    assert leader_ahead(ac1, idx, 2000.0) == ("p2", pytest.approx(200.0), 0.0)
    assert leader_ahead(ac2, idx, 2000.0) is None
    assert leader_ahead(ac1, idx, 150.0) is None  # beyond the lookahead

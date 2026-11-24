"""Simulated pilot behaviour (PLAN E3.3 T0 / T1).

A pilot is an agent that is assigned to one aircraft. It makes route choices,
accepts reroute suggestions, may change destination, and later handles spacing,
arrival metering and energy reserve decisions. All randomness is drawn from the
aircraft's own RNG stream.
"""

from __future__ import annotations

import numpy as np

from aerolink.network import Network
from aerolink.sim import pads as padlib
from aerolink.sim.demand import sample_next_trip_s
from aerolink.sim.events import (
    ARRIVAL,
    DEPARTURE,
    DEPARTURE_RELEASED,
    DEPARTURE_WAIT,
    DESTINATION_CHANGE,
    DESTINATION_CHANGE_REJECTED,
    DIVERSION,
    DIVERSION_UNAVAILABLE,
    HOLD_START,
    LANDING,
    PAD_REQUESTED,
    READY,
    ROUTE_UNAVAILABLE,
    STAND_IN,
    SUGGESTION,
    TRIP_REJECTED,
)
from aerolink.sim.fleet import choose_destination, pad_position
from aerolink.sim.physics import PAD_ALT_M, advance, build_legs, directional_alt_m
from aerolink.sim.reach import ROUTE_RETRY_S, can_get_home, fits
from aerolink.sim.separation import stack_hold_distance_m
from aerolink.sim.spacing import horizontal_m
from aerolink.sim.state import AIRBORNE, AircraftState, FlightLeg, FlightPlan, FlightPurpose, WorldState
from aerolink.sim.telemetry import seeded_uuid
from aerolink.vehicle import vehicle_cfg


class Pilot:
    def __init__(
        self,
        aircraft_id: str,
        network: Network,
        world: WorldState,
        advisor,
        cfg,
    ):
        self.aircraft_id = aircraft_id
        self.network = network
        self.world = world
        self.advisor = advisor
        self.cfg = cfg

    @property
    def ac(self) -> AircraftState:
        return self.world.aircraft[self.aircraft_id]

    @property
    def separation(self) -> bool:
        return self.cfg.world.separation

    def _legs(self, path: list[str], speeds: float | list[float]) -> list[FlightLeg]:
        return build_legs(path, self.network, speeds, directional=self.separation)

    def _hold_point_m(self, pad) -> float:
        """Remaining-route distance to hold at: the stack position with
        separation on (never behind the aircraft), else ``HOLD_DISTANCE_M``."""
        want = (
            stack_hold_distance_m(pad, self.aircraft_id, self.cfg.separation)
            if self.separation
            else padlib.HOLD_DISTANCE_M
        )
        return min(want, self.ac.remaining_route_m())

    def log(self, kind: str, **data) -> None:
        event = {"t": round(self.world.t, 6), "kind": kind, "aircraft_id": self.aircraft_id}
        event.update(data)
        self.world.events.append(event)

    # ---------------------------------------------------------------- departures
    def init_demand(self) -> None:
        ac = self.ac
        if ac.vehicle_type == "passenger" and ac.next_trip_s is None:
            ac.next_trip_s = sample_next_trip_s(0.0, self.cfg.start_local_hour, ac.rng, self.cfg.demand)

    def clear_off(self) -> None:
        """Move from the pad to a stand once ``PAD_CLEAR_S`` has passed since
        touchdown and a stand is free; retried every step until one is."""
        ac = self.ac
        if ac.on_stand or ac.pad_clear_at_s is None or self.world.t < ac.pad_clear_at_s:
            return
        assert ac.current_node is not None
        pad = self.world.pads[ac.current_node]
        if not pad.has_free_stand():
            return
        padlib.move_to_stand(pad, ac.aircraft_id)
        ac.on_stand = True
        ac.pad_clear_at_s = None
        self.log(STAND_IN, node=pad.node_id)

    def ready_to_leave(self) -> bool:
        """On a stand, departing needs a pad slot no queued arrival is waiting
        for. Logs ``departure_wait`` once per wait and ``departure_released`` when
        a slot frees up. Aircraft already on a pad can always go.

        With pad booking enabled, also respect ``ground_hold_until_s`` set when
        no arrival slot was available at trip time.
        """
        ac = self.ac
        assert ac.current_node is not None
        pad = self.world.pads[ac.current_node]
        if ac.ground_hold_until_s is not None and self.world.t < ac.ground_hold_until_s:
            if not ac.departure_waiting:
                ac.departure_waiting = True
                self.log("ground_hold", node=pad.node_id, until_s=round(ac.ground_hold_until_s, 6))
            return False
        if ac.ground_hold_until_s is not None and self.world.t >= ac.ground_hold_until_s:
            ac.ground_hold_until_s = None
            ac.departure_waiting = False
            self.log("ground_hold_end", node=pad.node_id)
        if self.separation and self._climb_out_blocked(pad):
            if not ac.departure_waiting:
                ac.departure_waiting = True
                self.log(DEPARTURE_WAIT, node=pad.node_id, queued=len(pad.queue), reason="traffic")
            return False
        if not ac.on_stand:
            if ac.departure_waiting:
                ac.departure_waiting = False
                self.log(DEPARTURE_RELEASED, node=pad.node_id)
            return True
        if pad.free_for_departure():
            if ac.departure_waiting:
                ac.departure_waiting = False
                self.log(DEPARTURE_RELEASED, node=pad.node_id)
            return True
        if not ac.departure_waiting:
            ac.departure_waiting = True
            self.log(DEPARTURE_WAIT, node=pad.node_id, queued=len(pad.queue))
        return False

    def _climb_out_blocked(self, pad) -> bool:
        """Departure release (separation on): no takeoff while another airborne
        aircraft is low over the site, inside the climb-out area. Aircraft
        queued for this pad are ignored: they wait for us to leave."""
        cfg = self.cfg.separation
        radius = cfg.follow_target_distance_m + cfg.min_separation_m
        ceiling = PAD_ALT_M + cfg.min_separation_m + cfg.min_vertical_separation_m
        node = self.network.nodes[pad.node_id]
        lat, lon = float(node["lat"]), float(node["lon"])
        for other in self.world.aircraft.values():
            if other.aircraft_id == self.aircraft_id or other.status not in AIRBORNE:
                continue
            if other.aircraft_id in pad.queue or other.position.alt_msl_m >= ceiling:
                continue
            if horizontal_m(lat, lon, other.position.lat, other.position.lon) < radius:
                return True
        return False

    def parked(self) -> None:
        self.clear_off()
        ac = self.ac
        if ac.vehicle_type == "passenger":
            if ac.next_trip_s is None or ac.next_trip_s > self.world.t:
                return
            if not self.ready_to_leave():
                return  # the trip stays due; retried next step
            dest = choose_destination(self.network, ac, ac.rng, self.cfg.demand.return_home_probability)
            first_failure = ac.retry_at_s is None
            ok = dest is not None and self._depart_with_booking(dest, purpose="passenger")
            ac.next_trip_s = sample_next_trip_s(self.world.t, self.cfg.start_local_hour, ac.rng, self.cfg.demand)
            if not ok:
                if first_failure:
                    self.log(TRIP_REJECTED, destination=dest)
        elif ac.current_node != ac.home_node:
            if ac.retry_at_s is not None and self.world.t < ac.retry_at_s:
                return
            if not self.ready_to_leave():
                return
            first_failure = ac.retry_at_s is None
            if self._depart_with_booking(ac.home_node, purpose="medical_return", log_unavailable=first_failure):
                ac.retry_at_s = None
            else:
                ac.retry_at_s = self.world.t + ROUTE_RETRY_S

    def _depart_with_booking(
        self,
        destination: str,
        purpose: FlightPurpose = "passenger",
        dispatch_t: float | None = None,
        planned_speeds: list[float] | None = None,
        log_unavailable: bool = True,
    ) -> bool:
        """Try to depart, booking an arrival slot first when pad_booking is on."""
        ac = self.ac
        origin = ac.current_node
        assert origin is not None
        booking_cfg = self.cfg.pad_booking if self.cfg.world.pad_booking else None
        if booking_cfg is not None:
            # Estimate a rough ETA using the advisor's best path and nominal speed.
            offered = self.advisor.request_routes(origin, destination, ac.vehicle_type, self.world.t)
            paths = [p for p in offered if self._path_fits(p)]
            if not paths:
                if log_unavailable:
                    self.log(ROUTE_UNAVAILABLE, origin=origin, destination=destination)
                return False
            path = paths[0]
            cruise = vehicle_cfg(ac.vehicle_type).cruise_speed_mps
            eta = self.world.t + self.network.route_length_m(path) / cruise
            slot = self._book_arrival_slot(destination, eta)
            if slot is None:
                # Ground-hold until the next slot opens.
                duration = self._slot_duration(destination)
                nxt = padlib.next_available_start(
                    self.world.pads[destination], self.world.t, duration, aircraft_id=ac.aircraft_id
                )
                if nxt is not None:
                    ac.ground_hold_until_s = nxt - self._flight_time_to(destination, nxt)
                    if ac.ground_hold_until_s <= self.world.t:
                        # We can leave immediately and still arrive at/after nxt.
                        slot = self._book_arrival_slot(destination, nxt)
                        if slot is not None:
                            return self.depart(
                                destination,
                                purpose=purpose,
                                dispatch_t=dispatch_t,
                                planned_speeds=planned_speeds,
                                pad_booking=slot,
                                log_unavailable=log_unavailable,
                            )
                        ac.ground_hold_until_s = None
                    else:
                        self.log("ground_hold", node=destination, until_s=round(ac.ground_hold_until_s, 6))
                return False
            return self.depart(
                destination,
                purpose=purpose,
                dispatch_t=dispatch_t,
                planned_speeds=planned_speeds,
                pad_booking=slot,
                log_unavailable=log_unavailable,
            )
        return self.depart(
            destination,
            purpose=purpose,
            dispatch_t=dispatch_t,
            planned_speeds=planned_speeds,
            log_unavailable=log_unavailable,
        )

    def _slot_duration(self, destination: str) -> float:
        """Time a landing aircraft blocks the destination pad.

        Covers final approach/landing plus turnaround (or pad clear if no stand).
        """
        return padlib.LANDING_DURATION_S + self.cfg.turnaround_s

    def _flight_time_to(self, destination: str, t_depart: float) -> float:
        """Best-case flight time estimate to ``destination`` at ``t_depart``."""
        origin = self.ac.current_node
        assert origin is not None
        paths = self.advisor.request_routes(origin, destination, self.ac.vehicle_type, t_depart)
        paths = [p for p in paths if self._path_fits(p)]
        if not paths:
            return 0.0
        return self.network.route_length_m(paths[0]) / vehicle_cfg(self.ac.vehicle_type).cruise_speed_mps

    def _book_arrival_slot(self, destination: str, eta: float) -> tuple[str, float, float] | None:
        """Book a pad slot around ``eta``. Returns (pad_id, t_start, t_end) or None."""
        ac = self.ac
        cfg = self.cfg.pad_booking
        pad = self.world.pads[destination]
        duration = self._slot_duration(destination)
        window = padlib.find_slot(pad, eta, duration, cfg.slot_tolerance_s, ac.aircraft_id)
        if window is None:
            return None
        t_start, t_end = window
        if not padlib.book_slot(pad, t_start, t_end, ac.aircraft_id):
            return None
        # Cancel any previous booking for this aircraft at any pad.
        if ac.booked_slot is not None and ac.booked_slot[0] != destination:
            padlib.cancel_booking(self.world.pads[ac.booked_slot[0]], ac.aircraft_id)
        ac.booked_slot = (destination, t_start, t_end)
        return ac.booked_slot

    def depart(
        self,
        destination: str,
        purpose: FlightPurpose = "passenger",
        dispatch_t: float | None = None,
        planned_speeds: list[float] | None = None,
        pad_booking: tuple[str, float, float] | None = None,
        log_unavailable: bool = True,
    ) -> bool:
        """Depart from the current pad to ``destination``.

        Only routes that fit the remaining battery range are flown.
        ``pad_booking`` = (pad_id, t_start, t_end) if a slot was pre-booked.
        """
        ac = self.ac
        origin = ac.current_node
        assert origin is not None
        if not self.ready_to_leave():
            return False
        offered = self.advisor.request_routes(origin, destination, ac.vehicle_type, self.world.t)
        paths = [p for p in offered if self._path_fits(p)]
        request_id = seeded_uuid(self.world.rng)
        if not paths:
            if log_unavailable:
                self.log(ROUTE_UNAVAILABLE, origin=origin, destination=destination)
            return False
        path = paths[0]
        # store the B1-style prediction if the advisor exposes one.
        if hasattr(self.advisor, "predict"):
            b1_option = self.advisor.predict(origin, destination, ac.vehicle_type, self.world.t)
            b1_option = [p for p in b1_option if self._path_fits(p.path)]
            predicted_total_s = b1_option[0].total_predicted_s if b1_option else None
        else:
            predicted_total_s = None
        cruise = vehicle_cfg(ac.vehicle_type).cruise_speed_mps
        sigma = self.cfg.cruise_speed_sigma
        if planned_speeds is None:
            planned_speeds = [
                cruise * float(np.clip(1.0 + ac.rng.normal(0.0, sigma), 0.5, 1.5)) for _ in range(len(path) - 1)
            ]
        if pad_booking is not None:
            # With metering on, adjust speeds to arrive at the slot opening.
            if self.cfg.world.arrival_metering:
                planned_speeds = self._metered_speeds(path, planned_speeds, pad_booking[1])
            ac.booked_slot = pad_booking
        legs = self._legs(path, planned_speeds)
        change_at = None
        if purpose == "passenger" and len(path) > 2 and ac.rng.random() < self.cfg.destination_change_probability:
            change_at = int(ac.rng.integers(1, len(path) - 1))
        plan = FlightPlan(
            flight_id=seeded_uuid(self.world.rng),
            request_id=request_id,
            route_id=seeded_uuid(self.world.rng),
            path=list(path),
            destination_node=destination,
            departure_time=self.world.t,
            eta_by_node=self._etas(legs, self.world.t),
            purpose=purpose,
            destination_change_at=change_at,
            dispatch_t=dispatch_t,
            flown=[origin],
        )
        if ac.on_stand:
            padlib.leave_stand(self.world.pads[origin], ac.aircraft_id)
            ac.on_stand = False
        else:
            padlib.release(self.world.pads[origin], ac.aircraft_id)
        ac.pad_clear_at_s = None
        ac.active_flight = plan
        ac.legs = legs
        ac.leg_index = 0
        ac.distance_along_edge_m = 0.0
        ac.velocity_mps = 0.0
        ac.vertical_speed_mps = 0.0
        ac.status = "climbing"
        ac.current_node = None
        ac.pad_requested = False
        ac.pad_cleared = False
        ac.hold_remaining_m = 0.0
        ac.predicted_total_s = predicted_total_s
        self.world.planned.append(plan)
        self.log(
            DEPARTURE,
            origin=origin,
            destination=destination,
            purpose=purpose,
            flight_id=str(plan.flight_id),
            path=list(path),
        )
        return True

    def _path_fits(self, path: list[str]) -> bool:
        ac = self.ac
        energy_cfg = self.cfg.energy if self.cfg.world.energy else None
        if energy_cfg is None:
            return fits(ac, self.network.route_length_m(path))
        from aerolink.sim.energy import trip_energy_estimate

        trip = trip_energy_estimate(self.network, path, ac.vehicle_type, energy_cfg)
        return trip <= ac.battery_soc - energy_cfg.reserve_soc + 1e-9

    def _route_fits(self, route_m: float) -> bool:
        energy_cfg = self.cfg.world.energy
        return fits(self.ac, route_m, self.cfg.energy if energy_cfg else None)

    @staticmethod
    def _etas(legs: list[FlightLeg], t0: float) -> dict[str, float]:
        etas: dict[str, float] = {}
        t = t0
        for leg in legs:
            t += leg.length_m / leg.planned_speed_mps
            etas[leg.end_node] = round(t, 6)
        return etas

    # ---------------------------------------------------------------- pad flow
    def _metered_speeds(self, path: list[str], planned_speeds: list[float], slot_open_s: float) -> list[float]:
        """Adjust cruise speeds so the aircraft reaches the destination when its
        booked slot opens, without leaving the vehicle's speed envelope.

        The adjustment is applied uniformly to every leg. If the slot cannot be
        met even at the speed limits, the original speeds are returned (the
        aircraft will hold on arrival or re-book).
        """
        vehicle = vehicle_cfg(self.ac.vehicle_type)
        min_speed = vehicle.min_cruise_speed_mps
        max_speed = vehicle.max_cruise_speed_mps
        route_m = self.network.route_length_m(path)
        if route_m <= 0:
            return planned_speeds
        # Time available from now until slot opens.
        available_s = slot_open_s - self.world.t
        if available_s <= 0:
            # Slot is already open; fly as fast as allowed.
            return [max_speed for _ in planned_speeds]
        required_mps = route_m / available_s
        if required_mps > max_speed:
            return [max_speed for _ in planned_speeds]
        if required_mps < min_speed:
            return [min_speed for _ in planned_speeds]
        return [required_mps for _ in planned_speeds]

    def request_pad(self) -> None:
        plan = self.ac.active_flight
        assert plan is not None
        pad = self.world.pads[plan.destination_node]
        self.ac.pad_requested = True
        granted = padlib.request(pad, self.aircraft_id)
        self.ac.pad_cleared = granted
        self.ac.stack_hold = self.separation
        self.log(PAD_REQUESTED, node=pad.node_id, granted=granted)
        if granted:
            self.log("pad_cleared", node=pad.node_id, from_queue=False)
        else:
            self.ac.hold_remaining_m = self._hold_point_m(pad)

    def cancel_pad_request(self) -> None:
        """Give up any slot or queue place at the current destination."""
        ac = self.ac
        if ac.active_flight is not None:
            plan = ac.active_flight
            pad = self.world.pads.get(plan.destination_node)
            if pad is not None:
                padlib.release(pad, self.aircraft_id)
                padlib.leave_queue(pad, self.aircraft_id)
        else:
            # No active flight: search all pads for this aircraft in queue/occupants.
            for pad in self.world.pads.values():
                if self.aircraft_id in pad.occupants:
                    padlib.release(pad, self.aircraft_id)
                if self.aircraft_id in pad.queue:
                    padlib.leave_queue(pad, self.aircraft_id)
        ac.pad_requested = False
        ac.pad_cleared = False
        ac.stack_hold = False
        ac.hold_remaining_m = 0.0

    def cancel_pad(self) -> None:
        """Give up any slot or queue place at the current destination."""
        plan = self.ac.active_flight
        assert plan is not None
        pad = self.world.pads[plan.destination_node]
        padlib.release(pad, self.aircraft_id)
        padlib.leave_queue(pad, self.aircraft_id)
        self.ac.pad_requested = False
        self.ac.pad_cleared = False
        self.ac.stack_hold = False
        self.ac.hold_remaining_m = 0.0

    # ---------------------------------------------------------------- on the pad
    def landing(self) -> None:
        ac = self.ac
        if ac.landing_until_s is not None and self.world.t < ac.landing_until_s:
            # Hover/landing drain while on final descent.
            if self.cfg.world.energy:
                vehicle = vehicle_cfg(ac.vehicle_type)
                ac.battery_soc = max(0.0, ac.battery_soc - vehicle.hover_soc_per_s * self.cfg.dt_s)
            return
        plan = ac.active_flight
        assert plan is not None
        node = plan.destination_node
        ac.position = pad_position(self.network, node)
        ac.vertical_speed_mps = 0.0
        ac.velocity_mps = 0.0
        ac.current_node = node
        ac.status = "turnaround"
        ac.landing_until_s = None
        ac.turnaround_until_s = self.world.t + self.cfg.turnaround_s
        ac.pad_clear_at_s = self.world.t + padlib.PAD_CLEAR_S
        data = {
            "node": node,
            "flight_id": str(plan.flight_id),
            "purpose": plan.purpose,
            "flight_time_s": round(self.world.t - plan.departure_time, 6),
            "diverted": plan.diverted,
            "destination_changed": plan.destination_changed,
            "accepted_suggestion": plan.accepted_suggestion,
        }
        if plan.dispatch_t is not None:
            data["response_time_s"] = round(self.world.t - plan.dispatch_t, 6)
        if ac.predicted_total_s is not None:
            data["eta_error_s"] = round(self.world.t - plan.departure_time - ac.predicted_total_s, 6)
            data["predicted_total_s"] = round(ac.predicted_total_s, 6)
        self.log(ARRIVAL, **data)
        ac.active_flight = None
        ac.legs = []
        ac.leg_index = 0
        ac.distance_along_edge_m = 0.0
        ac.level_swap_with = None
        ac.pad_requested = False
        ac.pad_cleared = False

    def turnaround(self) -> None:
        self.clear_off()
        ac = self.ac
        energy_cfg = self.cfg.energy if self.cfg.world.energy else None
        if energy_cfg is not None:
            # Gradual charging during turnaround.
            ac.battery_soc = min(1.0, ac.battery_soc + energy_cfg.charge_rate_soc_per_s * self.cfg.dt_s)
        if ac.turnaround_until_s is not None and self.world.t < ac.turnaround_until_s:
            return
        ac.status = "parked"
        ac.turnaround_until_s = None
        if energy_cfg is None:
            ac.battery_soc = 1.0
        self.log(READY, node=ac.current_node)
        if ac.vehicle_type == "passenger" and ac.next_trip_s is not None and ac.next_trip_s < self.world.t:
            ac.next_trip_s = self.world.t

    # ---------------------------------------------------------------- holding
    def holding(self) -> None:
        ac = self.ac
        ac.velocity_mps = 0.0
        ac.vertical_speed_mps = 0.0
        energy_cfg = self.cfg.energy if self.cfg.world.energy else None
        if energy_cfg is not None:
            # Hover drain while holding.
            vehicle = vehicle_cfg(ac.vehicle_type)
            ac.battery_soc = max(0.0, ac.battery_soc - vehicle.hover_soc_per_s * self.cfg.dt_s)
            from aerolink.sim.energy import nearest_alternate_energy

            needed = nearest_alternate_energy(self.network, ac, energy_cfg)
            if ac.battery_soc - needed <= energy_cfg.reserve_soc + 1e-9:
                self.divert_energy_reserve()
                return
        if self.separation and ac.pad_requested and not ac.pad_cleared:
            plan = ac.active_flight
            assert plan is not None
            want = self._hold_point_m(self.world.pads[plan.destination_node])
            if want < ac.hold_remaining_m - 1.0:
                # the queue moved up: fly forward to the new stack position
                ac.hold_remaining_m = want
                ac.status = "cruise"
                return
        held = self.world.t - (ac.hold_start_s if ac.hold_start_s is not None else self.world.t)
        if held >= self.cfg.max_hold_s and (ac.retry_at_s is None or self.world.t >= ac.retry_at_s):
            self.divert()

    def divert_energy_reserve(self) -> None:
        """Diversion triggered specifically by an energy reserve violation.

        Unlike :meth:`divert` this does not require a current flight plan; it
        picks the nearest alternate site that is reachable and can be landed on.
        """
        ac = self.ac

        energy_cfg = self.cfg.energy
        leg = ac.leg
        if leg is None:
            # No route context: use current node if at a site, otherwise hold.
            if ac.current_node is not None and self.network.nodes[ac.current_node].get("kind") != "waypoint":
                ac.status = "landing"
            return

        sites = self.network.nodes_for(ac.vehicle_type)
        candidates: list[tuple[float, str, list[str]]] = []
        dist_back = ac.distance_along_edge_m
        dist_forward = leg.length_m - ac.distance_along_edge_m
        for site in sites:
            d_start = self.network.route_distance_m(leg.start_node, site, ac.vehicle_type)
            d_end = self.network.route_distance_m(leg.end_node, site, ac.vehicle_type)
            options: list[tuple[float, list[str]]] = []
            if d_start is not None:
                start_paths = self.network.shortest_paths(leg.start_node, site, ac.vehicle_type, k=1)
                if start_paths:
                    options.append((dist_back + d_start, start_paths[0]))
            if d_end is not None:
                end_paths = self.network.shortest_paths(leg.end_node, site, ac.vehicle_type, k=1)
                if end_paths:
                    options.append((dist_forward + d_end, end_paths[0]))
            for ground, path in options:
                if len(path) < 2:
                    continue
                from aerolink.sim.energy import trip_energy_estimate

                trip = trip_energy_estimate(self.network, path, ac.vehicle_type, energy_cfg)
                if trip <= ac.battery_soc - energy_cfg.reserve_soc + 1e-9:
                    candidates.append((ground, site, path))

        if not candidates:
            self.log(DIVERSION_UNAVAILABLE)
            ac.retry_at_s = self.world.t + ROUTE_RETRY_S
            return

        candidates.sort(key=lambda x: x[0])
        _, _, path = candidates[0]
        self._divert_depart(path)

    def _divert_depart(self, path: list[str]) -> None:
        """Begin a diversion from the current holding position to ``path``."""
        ac = self.ac
        self.cancel_pad_request()
        speed = vehicle_cfg(ac.vehicle_type).cruise_speed_mps
        legs = self._legs(path, speed)
        plan = FlightPlan(
            flight_id=seeded_uuid(self.world.rng),
            request_id=seeded_uuid(self.world.rng),
            route_id=seeded_uuid(self.world.rng),
            path=list(path),
            destination_node=path[-1],
            departure_time=self.world.t,
            eta_by_node=self._etas(legs, self.world.t),
            purpose="reposition",
            destination_change_at=None,
            dispatch_t=None,
            flown=[],
            diverted=True,
            revision=1,
        )
        ac.active_flight = plan
        ac.legs = legs
        ac.leg_index = 0
        ac.distance_along_edge_m = 0.0
        ac.velocity_mps = 0.0
        ac.vertical_speed_mps = 0.0
        ac.status = "climbing"
        ac.current_node = None
        ac.pad_requested = False
        ac.pad_cleared = False
        ac.hold_remaining_m = 0.0
        self.world.planned.append(plan)
        self.log(DIVERSION, from_node=path[0], to_node=path[-1], flight_id=str(plan.flight_id))

    def divert(self) -> None:
        """Turn back along the current leg and fly to the nearest other site with a
        free slot that is within range/energy (and, for medical, from which home
        is reachable). Landing sites are never transited, so if the turn-back node
        is itself a site the only option is to land there.

        With no option the aircraft keeps holding, logs ``diversion_unavailable``
        once, and retries every ``ROUTE_RETRY_S``.
        """
        ac = self.ac
        plan = ac.active_flight
        assert plan is not None and ac.leg is not None
        old_dest = plan.destination_node
        leg = ac.leg
        back_node = leg.start_node
        back_m = ac.distance_along_edge_m  # distance to fly back to back_node
        energy_cfg = self.cfg.energy if self.cfg.world.energy else None
        if back_node in self.world.pads:
            options = [back_node]
        else:
            options = sorted(self.network.nodes_for(ac.vehicle_type))
        best: tuple[float, str, list[str]] | None = None
        for site in options:
            if site == old_dest or not self.world.pads[site].has_free_slot():
                continue
            if site == back_node:
                path = [back_node]
            else:
                paths = self.network.shortest_paths(back_node, site, vehicle_type=ac.vehicle_type, k=1)
                if not paths:
                    continue
                path = paths[0]
            d = back_m + self.network.route_length_m(path)
            if energy_cfg is None:
                ok_energy = fits(ac, d)
            else:
                from aerolink.sim.energy import trip_energy_estimate

                ok_energy = (
                    trip_energy_estimate(self.network, [back_node, *path[1:]], ac.vehicle_type, energy_cfg)
                    <= ac.battery_soc + 1e-6
                )
            if not ok_energy or not can_get_home(self.network, ac, site, energy_cfg):
                continue
            if best is None or d < best[0]:
                best = (d, site, path)
        if best is None:
            if ac.retry_at_s is None:
                self.log(DIVERSION_UNAVAILABLE, node=old_dest)
            ac.retry_at_s = self.world.t + ROUTE_RETRY_S
            return
        ac.retry_at_s = None
        _, site, path = best
        self.cancel_pad()
        back = FlightLeg(
            edge=leg.edge,
            start_node=leg.end_node,
            end_node=leg.start_node,
            cruise_alt_m=directional_alt_m(leg.edge, leg.end_node) if self.separation else leg.cruise_alt_m,
            planned_speed_mps=leg.planned_speed_mps,
        )
        along = leg.length_m - ac.distance_along_edge_m
        forward = self._legs(path, leg.planned_speed_mps) if len(path) > 1 else []
        ac.legs = [back, *forward]
        ac.leg_index = 0
        ac.distance_along_edge_m = along
        plan.destination_node = site
        plan.diverted = True
        plan.revision += 1
        plan.path = [*plan.flown, *path] if back_node != plan.flown[-1] else [*plan.flown, *path[1:]]
        ac.status = "cruise"
        ac.hold_start_s = None
        self.log(DIVERSION, from_node=old_dest, to_node=site, flight_id=str(plan.flight_id))

    # ---------------------------------------------------------------- flying
    def on_node(self, node: str) -> list[FlightLeg] | None:
        plan = self.ac.active_flight
        assert plan is not None
        plan.flown.append(node)
        speed = (
            self.ac.legs[self.ac.leg_index].planned_speed_mps
            if self.ac.leg
            else vehicle_cfg(self.ac.vehicle_type).cruise_speed_mps
        )
        if (
            plan.destination_change_at is not None
            and not plan.destination_changed
            and len(plan.flown) - 1 >= plan.destination_change_at
        ):
            plan.destination_change_at = None
            new = self.destination_change(node, speed)
            if new is not None:
                return new
        path = self.advisor.suggest(self.ac, node, self.world.t)
        if path is None or not fits(self.ac, self.network.route_length_m(path)) or self._u_turn(path):
            return None
        accepted = bool(self.ac.rng.random() < self.cfg.suggestion_acceptance_rate)
        self.log(SUGGESTION, node=node, path=list(path), accepted=accepted)
        if not accepted:
            return None
        plan.accepted_suggestion = True
        plan.revision += 1
        plan.path = [*plan.flown[:-1], *path]
        return self._legs(path, speed)

    def _u_turn(self, path: list[str]) -> bool:
        """With separation on, a reroute may not turn back onto the edge just
        flown: that would put the aircraft nose to nose with its followers."""
        plan = self.ac.active_flight
        assert plan is not None
        return self.separation and len(plan.flown) >= 2 and len(path) >= 2 and path[1] == plan.flown[-2]

    def destination_change(self, node: str, speed: float) -> list[FlightLeg] | None:
        plan = self.ac.active_flight
        assert plan is not None
        old = plan.destination_node

        def in_range(n: str) -> bool:
            d = self.network.route_distance_m(node, n, self.ac.vehicle_type)
            if d is None:
                return False
            energy_cfg = self.cfg.energy if self.cfg.world.energy else None
            if energy_cfg is None:
                return fits(self.ac, d)
            from aerolink.sim.energy import trip_energy_estimate

            paths = self.network.shortest_paths(node, n, vehicle_type=self.ac.vehicle_type, k=1)
            if not paths:
                return False
            trip = trip_energy_estimate(self.network, paths[0], self.ac.vehicle_type, energy_cfg)
            return trip <= self.ac.battery_soc - energy_cfg.reserve_soc + 1e-9

        candidates = [
            n
            for n in sorted(self.network.nodes_for(self.ac.vehicle_type))
            if n not in (old, plan.flown[0]) and in_range(n)
        ]
        if not candidates:
            return None
        new = candidates[int(self.ac.rng.integers(len(candidates)))]
        offered = self.advisor.request_routes(node, new, self.ac.vehicle_type, self.world.t)
        paths = [p for p in offered if self._path_fits(p) and not self._u_turn(p)]
        if not paths:
            self.log(DESTINATION_CHANGE_REJECTED, from_node=old, to_node=new)
            return None
        self.cancel_pad()
        plan.destination_node = new
        plan.destination_changed = True
        plan.revision += 1
        plan.path = [*plan.flown[:-1], *paths[0]]
        self.log(DESTINATION_CHANGE, node=node, from_node=old, to_node=new)
        return self._legs(paths[0], speed)

    def fly(self, dt: float) -> None:
        ac = self.ac
        plan = ac.active_flight
        assert plan is not None
        if self.separation and ac.pad_requested and not ac.pad_cleared:
            ac.hold_remaining_m = self._hold_point_m(self.world.pads[plan.destination_node])
        if not ac.pad_requested and ac.remaining_route_m() <= padlib.PAD_REQUEST_DISTANCE_M:
            self.request_pad()
        stop = 0.0 if ac.pad_cleared else ac.hold_remaining_m
        result = advance(
            ac,
            self.world.wind,
            dt,
            stop,
            on_node=lambda n: self.on_node(n),
            energy=self.cfg.energy if self.cfg.world.energy else None,
        )
        if not ac.pad_requested and ac.remaining_route_m() <= padlib.PAD_REQUEST_DISTANCE_M:
            self.request_pad()
            return
        if not result.at_stop:
            return
        if ac.pad_cleared and ac.remaining_route_m() <= 1e-6:
            if self.separation and ac.position.alt_msl_m > PAD_ALT_M + 1.0:
                return  # arrived high (from the holding stack): descend over the pad first
            ac.stack_hold = False
            ac.status = "landing"
            ac.landing_until_s = self.world.t + padlib.LANDING_DURATION_S
            ac.velocity_mps = 0.0
            ac.vertical_speed_mps = 0.0
            self.log(LANDING, node=plan.destination_node)
        elif not ac.pad_cleared:
            ac.status = "holding"
            ac.velocity_mps = 0.0
            ac.vertical_speed_mps = 0.0
            if ac.hold_start_s is None:  # not when moving up the holding stack
                ac.hold_start_s = self.world.t
                self.log(HOLD_START, node=plan.destination_node)

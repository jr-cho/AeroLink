"""Simulated vertiport/fleet operator (PLAN E3.2, split out in E3.3 T0).

The operator hands out pad slots from the FIFO queues and dispatches medical
flights to the nearest idle aircraft. It is part of the simulated world, not of
AeroLink, which never schedules or approves flights.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aerolink.network import Network
from aerolink.sim import pads as padlib
from aerolink.sim.demand import (
    ScheduleEntry,
    _seconds_to_time,
    _time_to_seconds,
    choose_incident_site,
    find_nearest_idle_medical_aircraft,
    next_medical_dispatch_s,
)
from aerolink.sim.events import (
    DISPATCH_EXPIRED,
    DISPATCH_ON_SITE,
    DISPATCH_UNSERVICEABLE,
    SCHEDULE_CONFLICT,
    SCHEDULED_DEPARTURE,
)
from aerolink.sim.reach import in_service_area
from aerolink.sim.state import WorldState

if TYPE_CHECKING:
    from aerolink.sim.controller import ControllerConfig


def _leg_key(entry: ScheduleEntry) -> tuple[str, str, str, str]:
    return (entry.aircraft, entry.origin, entry.destination, entry.departure_local)


class Operator:
    def __init__(self, network: Network, world: WorldState, cfg: ControllerConfig):
        self.network = network
        self.world = world
        self.cfg = cfg  # replaced by Controller.cfg's setter, so it never goes stale
        self.pending_dispatches: list[dict] = []
        self.next_dispatch_s = next_medical_dispatch_s(0.0, world.rng, cfg.demand.medical_dispatch_mean_interval_s)
        self.schedule: list[ScheduleEntry] = cfg.schedule if hasattr(cfg, "schedule") else []
        # scheduled legs held on the ground for a pad slot: they leave when the hold ends
        self.pending_scheduled: list[dict] = []
        self._incidents_raised: set[int] = set()

    @property
    def start_local_hour(self) -> float:
        return getattr(self.cfg, "start_local_hour", 7.0)

    def dispatch_medical(self, node_id: str, *, scripted: bool = False) -> None:
        """Scripted medical incident at ``node_id``; served by the nearest idle aircraft
        that can get there and home again. Outside the fleet's service area it is
        logged as ``dispatch_unserviceable`` and dropped."""
        if "medical" not in self.network.serves(node_id):
            raise ValueError(f"{node_id} is not a medical site")
        self._log("medical_incident", None, node=node_id, scripted=scripted)
        if not in_service_area(self.network, self.world.aircraft, node_id):
            self._log(DISPATCH_UNSERVICEABLE, None, node=node_id)
            return
        self.pending_dispatches.append({"node": node_id, "t": self.world.t})

    def _log(self, kind: str, aircraft_id: str | None = None, **data) -> None:
        event = {"t": round(self.world.t, 6), "kind": kind}
        if aircraft_id is not None:
            event["aircraft_id"] = aircraft_id
        event.update(data)
        self.world.events.append(event)

    def medical_demand(self) -> list[dict]:
        """Generate new incidents and return the list of pending dispatches."""
        self._scripted_incidents()
        cfg = self.cfg.demand
        while self.next_dispatch_s is not None and self.next_dispatch_s <= self.world.t:
            site = choose_incident_site(self.world, self.network, self.world.rng)
            if site is not None:
                self.pending_dispatches.append({"node": site, "t": self.next_dispatch_s})
                self._log("medical_incident", None, node=site)
            self.next_dispatch_s = next_medical_dispatch_s(
                self.next_dispatch_s, self.world.rng, cfg.medical_dispatch_mean_interval_s
            )
        return list(self.pending_dispatches)

    def _scripted_incidents(self) -> None:
        """Raise each scenario ``medical_incidents`` entry once, when its local time arrives."""
        for i, item in enumerate(getattr(self.cfg, "medical_incidents", [])):
            if i in self._incidents_raised:
                continue
            due_s = (_time_to_seconds(item["at_local"]) - self.start_local_hour * 3600.0) % 86_400.0
            if self.world.t >= due_s:
                self._incidents_raised.add(i)
                self.dispatch_medical(item["node"], scripted=True)

    def resolve_dispatches(self, depart_callback) -> None:
        """Assign each pending dispatch to the nearest idle medical aircraft. An
        idle aircraft already parked at the site (it landed there after the
        incident was raised) serves it without a flight. With
        ``medical_dispatch_timeout_s`` set, dispatches nobody has taken in time
        are dropped as ``dispatch_expired``."""
        timeout = self.cfg.demand.medical_dispatch_timeout_s
        still: list[dict] = []
        for d in self.pending_dispatches:
            if timeout is not None and self.world.t - d["t"] > timeout:
                self._log(DISPATCH_EXPIRED, None, node=d["node"], waited_s=round(self.world.t - d["t"], 6))
                continue
            energy_cfg = self.cfg.energy if self.cfg.world.energy else None
            aid = find_nearest_idle_medical_aircraft(self.world, d["node"], self.network, energy_cfg)
            if aid is None:
                still.append(d)
                continue
            ac = self.world.aircraft[aid]
            if ac.current_node == d["node"]:
                self._log(DISPATCH_ON_SITE, aid, node=d["node"], response_time_s=round(self.world.t - d["t"], 6))
                continue
            # A dispatch that can't leave yet (e.g. ground hold) is retried every step; its
            # "route unavailable" is one event per dispatch, not one per retry.
            if not depart_callback(
                ac,
                d["node"],
                purpose="medical_dispatch",
                dispatch_t=d["t"],
                log_unavailable=not d.get("unavailable_logged"),
            ):
                d["unavailable_logged"] = True
                still.append(d)
        self.pending_dispatches = still

    def grant_pads(self) -> None:
        """Hand free slots to queued aircraft; low-energy emergencies go first."""
        for nid in sorted(self.world.pads):
            pad = self.world.pads[nid]
            urgent = {a for a in pad.queue if self.world.aircraft[a].low_energy_emergency}
            for aid in padlib.grant_from_queue(pad, urgent):
                ac = self.world.aircraft[aid]
                ac.pad_cleared = True
                extra = {"priority": True} if aid in urgent else {}
                self._log("pad_cleared", aid, node=nid, from_queue=True, **extra)
                if ac.hold_start_s is not None:  # holding, or moving up the holding stack
                    held = self.world.t - ac.hold_start_s
                    self._log("hold_end", aid, node=nid, held_s=round(held, 6))
                    if ac.status == "holding":
                        ac.status = "descending"
                    ac.hold_start_s = None
                    ac.retry_at_s = None

    def clear_pads_for_emergencies(self, reposition_callback) -> None:
        """A low-energy emergency is queued at a site whose pads are all taken: ask one idle aircraft
        parked on a pad (no stand free for it) to fly to the nearest site with room, so the slot
        opens for the emergency. ``reposition_callback(ac, site)`` departs it."""
        for nid in sorted(self.world.pads):
            pad = self.world.pads[nid]
            if pad.has_free_slot() or not any(self.world.aircraft[a].low_energy_emergency for a in pad.queue):
                continue
            idle = [
                a
                for a in sorted(pad.occupants)
                if (ac := self.world.aircraft.get(a)) is not None
                and ac.status == "parked"
                and ac.current_node == nid
                and not ac.on_stand
                and not (ac.scheduled_until_s is not None and ac.scheduled_until_s > self.world.t)
            ]
            if not idle:
                continue
            ac = self.world.aircraft[idle[0]]
            site = self._nearest_site_with_room(ac, nid)
            if site is not None:
                reposition_callback(ac, site)

    def _nearest_site_with_room(self, ac, here: str) -> str | None:
        from aerolink.sim.reach import path_fits

        energy_cfg = self.cfg.energy if self.cfg.world.energy else None
        best: tuple[float, str] | None = None
        for site in sorted(self.network.nodes_for(ac.vehicle_type)):
            if site == here or not padlib.has_parking(self.world.pads[site]):
                continue
            paths = self.network.shortest_paths(here, site, vehicle_type=ac.vehicle_type, k=1)
            if not paths or not path_fits(self.network, ac, paths[0], energy_cfg):
                continue
            d = self.network.route_length_m(paths[0])
            if best is None or d < best[0]:
                best = (d, site)
        return best[1] if best else None

    def handle_schedule(self, depart_callback) -> list[str]:
        """Trigger scheduled departures whose local time has arrived.

        The assigned aircraft (by id or type) is reserved until the departure
        time.  If unavailable at that moment, the operator tries one alternate
        of the same type; if no aircraft can depart, the leg is skipped and
        ``schedule_conflict`` is logged.
        """
        self._retry_scheduled(depart_callback)
        if not self.schedule:
            return []
        start_t = self.start_local_hour * 3600.0
        day_elapsed = (self.world.t + start_t) % 86_400.0
        # one key per leg: two legs from different sites at the same time must both fire
        fired: set[tuple[str, str, str, str]] = set()
        for entry in self.schedule:
            since = day_elapsed - entry.departure_seconds
            if since < 0:
                continue
            if entry.repeat_every_s:
                since %= entry.repeat_every_s  # a shuttle: the leg recurs every ``repeat_every_s``
            if since >= self.cfg.dt_s:
                continue  # not this step: fire once, within the first step window of each departure time
            leg = entry
            if entry.repeat_every_s:
                leg = ScheduleEntry(
                    entry.aircraft,
                    entry.origin,
                    entry.destination,
                    _seconds_to_time(round(day_elapsed - since)),
                    entry.purpose,
                    None,
                )
            if _leg_key(leg) in fired:
                continue
            fired.add(_leg_key(leg))
            if not self._dispatch_scheduled(leg, depart_callback):
                self._log(
                    SCHEDULE_CONFLICT,
                    None,
                    aircraft=leg.aircraft,
                    origin=leg.origin,
                    destination=leg.destination,
                    departure_local=leg.departure_local,
                )
        return sorted(key[3] for key in fired)

    def _dispatch_scheduled(self, entry: ScheduleEntry, depart_callback) -> bool:
        """Try to depart one scheduled leg, possibly reassigning aircraft."""
        # Prefer an aircraft that can fly the leg with the charge it has (the one named in the entry
        # first); else one that can once charged: the leg then waits for the battery, as a passenger trip does.
        ready = charging = None
        for aid in sorted(self.world.aircraft):
            ac = self.world.aircraft[aid]
            if not entry.matches_aircraft(ac):
                continue
            if ac.status != "parked" or ac.current_node != entry.origin:
                continue
            if ac.scheduled_until_s is not None and ac.scheduled_until_s > self.world.t:
                continue
            fits_now, fits_full = self._leg_fit(ac, entry)
            if fits_now and (ready is None or ac.aircraft_id == entry.aircraft):
                ready = ac
            elif fits_full and not fits_now and charging is None:
                charging = ac
        best = ready or charging
        if best is None:
            return False
        self._log(
            SCHEDULED_DEPARTURE,
            best.aircraft_id,
            origin=entry.origin,
            destination=entry.destination,
            departure_local=entry.departure_local,
            assigned=entry.aircraft,
        )
        return self._depart_scheduled(best, entry, self.world.t, depart_callback)

    def _leg_fit(self, ac, entry: ScheduleEntry) -> tuple[bool, bool]:
        """(fits the aircraft's charge now, fits a full battery) for a scheduled leg; an unreachable
        leg is (False, False) and is left to the depart callback to refuse."""
        from aerolink.sim.reach import path_fits

        energy_cfg = self.cfg.energy if self.cfg.world.energy else None
        paths = self.network.shortest_paths(entry.origin, entry.destination, vehicle_type=ac.vehicle_type, k=1)
        if not paths:
            return True, True
        return (
            path_fits(self.network, ac, paths[0], energy_cfg),
            path_fits(self.network, ac, paths[0], energy_cfg, soc=1.0),
        )

    def _depart_scheduled(self, ac, entry: ScheduleEntry, requested_t: float, depart_callback) -> bool:
        """Depart a scheduled leg. If the only thing in the way is a wait (a ground hold for a destination
        pad slot, or no free pad to take off from), the leg is kept, the aircraft stays reserved, and it
        leaves when the wait ends: the passengers wait on the ground instead of the leg being dropped."""
        if depart_callback(ac, entry.destination, purpose=entry.purpose, dispatch_t=requested_t):
            ac.scheduled_until_s = None
            return True
        fits_now, fits_full = self._leg_fit(ac, entry)
        waiting = (
            ac.departure_waiting
            or (ac.ground_hold_until_s is not None and ac.ground_hold_until_s > self.world.t)
            or (fits_full and not fits_now)  # charging
        )
        if waiting:
            ac.scheduled_until_s = requested_t + self.cfg.max_hold_s
            if not any(p["aircraft_id"] == ac.aircraft_id for p in self.pending_scheduled):
                self.pending_scheduled.append({"aircraft_id": ac.aircraft_id, "entry": entry, "t": requested_t})
            return True
        return False

    def _retry_scheduled(self, depart_callback) -> None:
        """Retry held scheduled legs. One that cannot leave within ``max_hold_s`` of its schedule
        time is dropped as ``schedule_conflict``, as an unserved leg always was."""
        still: list[dict] = []
        for p in self.pending_scheduled:
            ac = self.world.aircraft[p["aircraft_id"]]
            entry: ScheduleEntry = p["entry"]
            held = ac.ground_hold_until_s is not None and self.world.t < ac.ground_hold_until_s
            if ac.status != "parked" or ac.current_node != entry.origin or self.world.t - p["t"] > self.cfg.max_hold_s:
                ac.scheduled_until_s = None
                self._log(
                    SCHEDULE_CONFLICT,
                    None,
                    aircraft=entry.aircraft,
                    origin=entry.origin,
                    destination=entry.destination,
                    departure_local=entry.departure_local,
                )
                continue
            if held or not depart_callback(
                ac, entry.destination, purpose=entry.purpose, dispatch_t=p["t"], log_unavailable=False
            ):
                still.append(p)
            else:
                ac.scheduled_until_s = None
        self.pending_scheduled = still

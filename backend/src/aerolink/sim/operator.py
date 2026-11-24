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


class Operator:
    def __init__(self, network: Network, world: WorldState, cfg: ControllerConfig):
        self.network = network
        self.world = world
        self.cfg = cfg  # replaced by Controller.cfg's setter, so it never goes stale
        self.pending_dispatches: list[dict] = []
        self.next_dispatch_s = next_medical_dispatch_s(0.0, world.rng, cfg.demand.medical_dispatch_mean_interval_s)
        self.schedule: list[ScheduleEntry] = cfg.schedule if hasattr(cfg, "schedule") else []

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
            aid = find_nearest_idle_medical_aircraft(self.world, d["node"], self.network)
            if aid is None:
                still.append(d)
                continue
            ac = self.world.aircraft[aid]
            if ac.current_node == d["node"]:
                self._log(DISPATCH_ON_SITE, aid, node=d["node"], response_time_s=round(self.world.t - d["t"], 6))
                continue
            if not depart_callback(ac, d["node"], purpose="medical_dispatch", dispatch_t=d["t"]):
                still.append(d)
        self.pending_dispatches = still

    def grant_pads(self) -> None:
        """Hand free slots to queued aircraft."""
        for nid in sorted(self.world.pads):
            for aid in padlib.grant_from_queue(self.world.pads[nid]):
                ac = self.world.aircraft[aid]
                ac.pad_cleared = True
                self._log("pad_cleared", aid, node=nid, from_queue=True)
                if ac.hold_start_s is not None:  # holding, or moving up the holding stack
                    held = self.world.t - ac.hold_start_s
                    self._log("hold_end", aid, node=nid, held_s=round(held, 6))
                    if ac.status == "holding":
                        ac.status = "descending"
                    ac.hold_start_s = None
                    ac.retry_at_s = None

    def handle_schedule(self, depart_callback) -> list[str]:
        """Trigger scheduled departures whose local time has arrived.

        The assigned aircraft (by id or type) is reserved until the departure
        time.  If unavailable at that moment, the operator tries one alternate
        of the same type; if no aircraft can depart, the leg is skipped and
        ``schedule_conflict`` is logged.
        """
        if not self.schedule:
            return []
        start_t = self.start_local_hour * 3600.0
        day_elapsed = (self.world.t + start_t) % 86_400.0
        fired: set[str] = set()
        for entry in self.schedule:
            dep = entry.departure_seconds
            # Fire once within the second window for this departure.
            if dep <= day_elapsed < dep + self.cfg.dt_s and entry.departure_local not in fired:
                repeats = [entry]
                if entry.repeat_every_s:
                    k = 1
                    while dep + k * entry.repeat_every_s < 86_400.0:
                        repeats.append(
                            ScheduleEntry(
                                entry.aircraft,
                                entry.origin,
                                entry.destination,
                                _seconds_to_time(dep + k * entry.repeat_every_s),
                                entry.purpose,
                                None,
                            )
                        )
                        k += 1
                for rep in repeats:
                    if rep.departure_local in fired:
                        continue
                    if not self._dispatch_scheduled(rep, depart_callback):
                        self._log(
                            SCHEDULE_CONFLICT,
                            None,
                            aircraft=rep.aircraft,
                            origin=rep.origin,
                            destination=rep.destination,
                            departure_local=rep.departure_local,
                        )
                    fired.add(rep.departure_local)
        return list(fired)

    def _dispatch_scheduled(self, entry: ScheduleEntry, depart_callback) -> bool:
        """Try to depart one scheduled leg, possibly reassigning aircraft."""
        best = None
        for aid in sorted(self.world.aircraft):
            ac = self.world.aircraft[aid]
            if not entry.matches_aircraft(ac):
                continue
            if ac.status != "parked" or ac.current_node != entry.origin:
                continue
            if ac.scheduled_until_s is not None and ac.scheduled_until_s > self.world.t:
                continue
            # prefer the directly assigned aircraft
            if ac.aircraft_id == entry.aircraft:
                best = ac
                break
            if best is None:
                best = ac
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
        return depart_callback(best, entry.destination, purpose=entry.purpose, dispatch_t=self.world.t)
